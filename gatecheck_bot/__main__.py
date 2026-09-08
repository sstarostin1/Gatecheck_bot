"""Точка входа Gatecheck Bot: запуск long polling через aiogram 3."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from . import __version__
from .config import ConfigError, Settings
from .handlers import build_router

logger = logging.getLogger("gatecheck_bot")

GET_ME_TIMEOUT_SECONDS = 20

# Ошибки socks/http-коннектора прокси (aiohttp_socks) — НЕ наследники OSError и
# не aiohttp.ClientError, поэтому их нужно ловить явно, иначе вместо внятного
# сообщения пользователь получит traceback.
PROXY_CONNECT_ERRORS: tuple[type[Exception], ...] = (
    ProxyError,
    ProxyConnectionError,
    ProxyTimeoutError,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def mask_proxy_url(proxy_url: str) -> str:
    """Скрыть логин/пароль в URL прокси — для безопасного вывода в лог."""
    parts = urlsplit(proxy_url)
    host = parts.hostname or "?"
    if ":" in host:
        host = f"[{host}]"  # IPv6-литерал
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def build_session(settings: Settings) -> AiohttpSession:
    """Сессия Telegram API: при заданном PROXY_URL весь трафик идёт через прокси."""
    if not settings.proxy_url:
        logger.info("PROXY_URL не задан — прямое соединение с api.telegram.org.")
        return AiohttpSession()
    logger.info(
        "Трафик к api.telegram.org пойдёт через прокси: %s",
        mask_proxy_url(settings.proxy_url),
    )
    return AiohttpSession(proxy=settings.proxy_url)


def network_error_text(settings: Settings, exc: Exception) -> str:
    """Понятное сообщение о сетевой проблеме при старте (с подсказкой про прокси)."""
    detail = f"{type(exc).__name__}: {exc}"
    if settings.proxy_url:
        return (
            f"Не удалось связаться с api.telegram.org через прокси за "
            f"{GET_ME_TIMEOUT_SECONDS} с ({detail}). Проверь, жив ли прокси и верна "
            "ли запись PROXY_URL в .env."
        )
    return (
        f"Не удалось связаться с api.telegram.org за {GET_ME_TIMEOUT_SECONDS} с ({detail}). "
        "Похоже, Telegram недоступен из твоей сети напрямую. Задай PROXY_URL в .env "
        "(http://... или socks5://...), примеры — в .env.example и README."
    )


async def run(settings: Settings) -> None:
    """Поднять Bot+Dispatcher и крутить long polling до прерывания."""
    bot = Bot(token=settings.bot_token, session=build_session(settings))
    dp = Dispatcher()
    dp.include_router(build_router(settings))

    try:
        logger.info("Проверяю связь с Telegram (get_me, таймаут %d с)...", GET_ME_TIMEOUT_SECONDS)
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=GET_ME_TIMEOUT_SECONDS)
        except TelegramUnauthorizedError as exc:
            raise RuntimeError(
                "Telegram отклонил токен (401 Unauthorized). Проверь BOT_TOKEN в .env "
                "(токен выдаёт @BotFather)."
            ) from exc
        except (
            TimeoutError,
            TelegramNetworkError,
            OSError,
            *PROXY_CONNECT_ERRORS,
        ) as exc:
            raise RuntimeError(network_error_text(settings, exc)) from exc
        logger.info("Gatecheck Bot v%s запущен как @%s (long polling)", __version__, me.username)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


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
