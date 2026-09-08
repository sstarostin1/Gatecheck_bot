"""Живая проверка кандидатов: реальный HTTPS-запрос к api.telegram.org через прокси.

«TCP-порт открыт» — недостаточно: в списках много живых хостов, которые не
проксируют Telegram. Критерий здесь — любой HTTP-ответ от api.telegram.org
через сам прокси: значит, TLS-хендшейк до сервера Telegram прошёл.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import aiohttp
from aiohttp_socks import ProxyConnector

from .proxy_types import VLESS, ProxyInfo
from .xray import XrayManager

logger = logging.getLogger("gatecheck_bot.transport")

TG_PROBE_URL = "https://api.telegram.org/"
TCP_PRECHECK_TIMEOUT = 2.0  # отсев мёртвых хостов до полной пробы


async def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    """Быстрый TCP-пробой в executor'е: отменяемый, не зависит от Proactor-капризов."""
    loop = asyncio.get_running_loop()

    def _check() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _check), timeout + 2)
    except (TimeoutError, OSError):
        return False


async def check_connector_url(url: str, timeout: float) -> float | None:
    """Проба api.telegram.org через готовый connector-URL; вернуть latency (сек)."""
    started = time.monotonic()
    try:
        connector = ProxyConnector.from_url(url, rdns=True)
        async with aiohttp.ClientSession(connector=connector) as session, session.get(
            TG_PROBE_URL, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            await resp.read()
        return time.monotonic() - started
    except Exception as exc:
        logger.debug("Проба %s не прошла: %s: %s", url, type(exc).__name__, exc)
        return None


async def check_proxy(info: ProxyInfo, timeout: float, xray: XrayManager) -> float | None:
    """Проверить кандидата; для vless — поднять xray и пробовать локальный порт."""
    if not await _tcp_reachable(info.host, info.port, TCP_PRECHECK_TIMEOUT):
        return None
    if info.kind == VLESS:
        port = await xray.start(info.uri)
        if port is None:
            return None
        info.local_port = port
    return await check_connector_url(info.connector_url, timeout)
