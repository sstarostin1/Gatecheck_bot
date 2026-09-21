"""Точка входа Gatecheck Bot: запуск long polling через aiogram 3.

Транспортная стратегия (устойчивость к блокировкам Telegram):
1. Статичный прокси из PROXY_URL (.env) — если задан, пробуется первым.
2. Прямое соединение — штатный путь (например, VPS за границей).
3. Автономный прокси-пул (gatecheck_bot.transport): кандидаты из открытых
   списков проверяются реальным запросом к api.telegram.org через сам прокси,
   рабочий пул кэшируется; вотчдог следит за каналом и ротирует прокси.
   Пул исчерпан — добыча запускается заново (источники доступны без Telegram).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from . import __version__
from .config import ConfigError, Settings
from .handlers import _get_gate_labels, _get_gates, _get_graph, build_router
from .monitoring import RouteMonitor
from .storage import Storage
from .transport import ProxyPool, mask_proxy_url
from .transport.checker import TG_PROBE_URL
from .transport.xray import XrayManager
from .zone import ZoneMonitor

logger = logging.getLogger("gatecheck_bot")

GET_ME_TIMEOUT_SECONDS = 20
DIRECT_PROBE_TIMEOUT = 6.0      # быстрая проба прямого доступа, сек
WATCHDOG_INTERVAL = 60.0        # период проверки канала вотчдогом, сек
WATCHDOG_TIMEOUT = 20.0         # таймаут пробы вотчдога, сек
WATCHDOG_MAX_FAILURES = 2       # сколько проб подряд считать «канал мёртв»
POOL_RETRY_DELAY = 60.0         # пауза между попытками добычи, сек

# Ошибки socks/http-коннектора прокси (aiohttp_socks) — НЕ наследники OSError и
# не aiohttp.ClientError, поэтому их нужно ловить явно.
PROXY_CONNECT_ERRORS: tuple[type[Exception], ...] = (
    ProxyError,
    ProxyConnectionError,
    ProxyTimeoutError,
)


class NetworkDead(RuntimeError):
    """Устойчивый сетевой сбой текущего транспорта — нужна ротация."""


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def build_session(proxy_url: str | None) -> AiohttpSession:
    """Сессия Telegram API: при заданном URL весь трафик идёт через прокси."""
    if not proxy_url:
        logger.info("Транспорт: прямое соединение с api.telegram.org.")
        return AiohttpSession()
    logger.info("Транспорт: прокси %s для api.telegram.org.", mask_proxy_url(proxy_url))
    return AiohttpSession(proxy=proxy_url)


def network_error_text(proxy_url: str | None, exc: Exception) -> str:
    """Понятное сообщение о сетевой проблеме при старте транспорта."""
    detail = f"{type(exc).__name__}: {exc}"
    if proxy_url:
        return (
            f"Транспорт {mask_proxy_url(proxy_url)} не смог связаться с api.telegram.org "
            f"за {GET_ME_TIMEOUT_SECONDS} с ({detail})."
        )
    return (
        f"Не удалось связаться с api.telegram.org напрямую за {GET_ME_TIMEOUT_SECONDS} с "
        f"({detail}). Если это не временный сбой — задай PROXY_URL в .env или используй "
        "автономный пул (PROXY_AUTOPOOL=1, включен по умолчанию)."
    )


async def probe_direct(timeout: float = DIRECT_PROBE_TIMEOUT) -> bool:
    """Быстрая проверка прямого доступа: любой HTTP-ответ = канал до Telegram есть."""
    try:
        async with aiohttp.ClientSession() as session, session.get(
            TG_PROBE_URL, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            await resp.read()
        return True
    except Exception:
        return False


async def run_transport(
    settings: Settings,
    proxy_url: str | None,
    label: str,
    monitor: RouteMonitor,
    zone_monitor: ZoneMonitor,
) -> None:
    """Один «цикл жизни» сессии: get_me → polling под вотчдогом.

    Штатное завершение (stop_polling / Ctrl+C) — просто возврат.
    Устойчивый сетевой сбой — NetworkDead (наружному циклу нужна ротация).
    Прочие исключения фатальны (конфиг/токен) и уходят наружу как есть.
    """
    bot = Bot(
        token=settings.bot_token,
        session=build_session(proxy_url),
        # §0.1: HTML parse mode — <b>/<i>/<pre>/<blockquote expandable> во всех сообщениях.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(build_router(settings, monitor, zone_monitor))
    polling_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    monitor_task: asyncio.Task | None = None
    zone_task: asyncio.Task | None = None

    async def watchdog() -> None:
        failures = 0
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            try:
                await asyncio.wait_for(bot.get_me(), timeout=WATCHDOG_TIMEOUT)
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning(
                    "Вотчдог: транспорт %s не отвечает (%d/%d): %s",
                    label, failures, WATCHDOG_MAX_FAILURES, type(exc).__name__,
                )
                if failures >= WATCHDOG_MAX_FAILURES:
                    raise NetworkDead(f"Транспорт {label} перестал отвечать") from exc

    try:
        logger.info("Проверяю связь с Telegram (get_me, таймаут %d с)...", GET_ME_TIMEOUT_SECONDS)
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=GET_ME_TIMEOUT_SECONDS)
        except TelegramUnauthorizedError as exc:
            raise RuntimeError(
                "Telegram отклонил токен (401 Unauthorized). Проверь BOT_TOKEN в .env "
                "(токен выдаёт @BotFather)."
            ) from exc
        except (TimeoutError, TelegramNetworkError, OSError, *PROXY_CONNECT_ERRORS) as exc:
            raise NetworkDead(network_error_text(proxy_url, exc)) from exc

        logger.info(
            "Gatecheck Bot v%s запущен как @%s (транспорт: %s)",
            __version__, me.username, label,
        )
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        watchdog_task = asyncio.create_task(watchdog())
        monitor_task = asyncio.create_task(monitor.run_forever(bot))
        zone_task = asyncio.create_task(zone_monitor.run_forever(bot))
        done, _pending = await asyncio.wait(
            {polling_task, watchdog_task, monitor_task, zone_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        return  # polling завершился штатно
    finally:
        for task in (polling_task, watchdog_task, monitor_task, zone_task):
            if task is not None:
                task.cancel()
        pending = [
            task
            for task in (polling_task, watchdog_task, monitor_task, zone_task)
            if task is not None
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await bot.session.close()


async def run(settings: Settings) -> None:
    """Выбрать живой транспорт и крутить polling; при сбоях — ротация (см. модуль)."""
    xray = XrayManager()
    storage: Storage | None = None
    try:
        storage = Storage(settings.db_path)
    except Exception as exc:
        logger.error("SQLite недоступен (%s) — работаем в памяти, рестарт потеряет слежки.", exc)
    monitor = RouteMonitor(storage=storage)  # живёт через ротации транспортов
    zone_monitor = ZoneMonitor(storage=storage)
    pool: ProxyPool | None = None
    if settings.proxy_autopool:
        pool = ProxyPool(
            sources=settings.proxy_sources,
            manual=settings.proxy_manual,
            cache_path=settings.proxy_cache_path,
            pool_size=settings.proxy_pool_size,
            check_timeout=settings.proxy_check_timeout,
            xray=xray,
        )

    try:
        # Восстановить слежки из SQLite до подключения (рестарт-персистентность).
        graph = _get_graph()
        gate_index, gate_names, gate_dest = _get_gates()
        graph = _get_graph()
        gate_labels = _get_gate_labels(graph) if graph is not None else {}
        if graph is not None and storage is not None:
            zones_restored = zone_monitor.restore(graph, gate_index, gate_names, gate_labels)
            routes_restored = monitor.restore(graph, gate_index, gate_names, gate_dest)
            if zones_restored or routes_restored:
                logger.info(
                    "Восстановлено слежек после рестарта: зон %d, маршрутов %d.",
                    zones_restored, routes_restored,
                )

        if settings.proxy_url:
            static_label = mask_proxy_url(settings.proxy_url)
            logger.info("Транспорт №1: статичный прокси %s.", static_label)
            try:
                await run_transport(
                    settings, settings.proxy_url, static_label, monitor, zone_monitor
                )
                return
            except NetworkDead:
                logger.error("Статичный прокси не отвечает — пробую следующие транспорты.")

        if await probe_direct():
            logger.info("Транспорт №2: прямое соединение.")
            try:
                await run_transport(settings, None, "напрямую", monitor, zone_monitor)
                return
            except NetworkDead:
                logger.error("Прямое соединение потеряно — включаю автономный прокси-пул.")
        else:
            logger.info("Прямой доступ к api.telegram.org отсутствует — включаю прокси-пул.")

        if pool is None:
            raise RuntimeError(
                "api.telegram.org недоступен напрямую, а автономный пул отключен "
                "(PROXY_AUTOPOOL=0). Задай PROXY_URL в .env или включи пул."
            )

        while True:
            working = await pool.refresh()
            if working == 0:
                logger.error(
                    "Рабочих прокси не найдено — повторю добычу через %.0f с.",
                    POOL_RETRY_DELAY,
                )
                await asyncio.sleep(POOL_RETRY_DELAY)
                continue
            while True:
                proxy = await pool.next()
                if proxy is None:
                    logger.warning("Пул исчерпан — обновляю источники.")
                    break
                try:
                    await run_transport(
                        settings, proxy.connector_url, proxy.label(), monitor, zone_monitor
                    )
                    return
                except NetworkDead:
                    logger.warning("Прокси %s перестал работать — беру следующий.", proxy.label())
            await asyncio.sleep(5.0)
    finally:
        await xray.stop()
        await monitor.aclose()
        await zone_monitor.aclose()
        if storage is not None:
            storage.close()


def main() -> None:
    setup_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logger.error("Конфигурация: %s", exc)
        raise SystemExit(2) from None

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C).")
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
