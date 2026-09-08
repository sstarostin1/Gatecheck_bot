"""Добыча кандидатов: открытые списки по обычному HTTPS (Telegram не нужен).

Именно это делает цикл автономным: даже при полностью заблокированном Telegram
источники списков (GitHub raw и т.п.) доступны, поэтому бот сам добывает и
проверяет прокси. Новый источник = ещё один URL в PROXY_SOURCES.
"""

from __future__ import annotations

import base64
import binascii
import logging

import aiohttp

from .proxy_types import DEFAULT_SOURCES, ProxyInfo, parse_proxy_line

logger = logging.getLogger("gatecheck_bot.transport")

_UA = "GatecheckBot/0.3 (proxy pool discovery)"

__all__ = ["DEFAULT_SOURCES", "ProxyInfo", "extract_candidates", "fetch_text"]


async def fetch_text(url: str, timeout: float = 20.0) -> str | None:
    """Скачать список как текст; при сетевом сбое — None (источник пропускаем)."""
    try:
        async with aiohttp.ClientSession() as session, session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": _UA},
        ) as resp:
            return (await resp.read()).decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("Источник недоступен %s: %s", url, type(exc).__name__)
        return None


def _maybe_base64_decode(text: str) -> str:
    """Часть подписок отдаёт base64-блоб вместо построчного списка."""
    stripped = "".join(text.split())
    if "://" in text or len(stripped) < 64 or not stripped.isascii():
        return text
    try:
        padded = stripped + "=" * (-len(stripped) % 4)
        return base64.b64decode(padded, validate=True).decode("utf-8", errors="ignore")
    except (binascii.Error, ValueError):
        return text


def extract_candidates(text: str, source: str) -> list[ProxyInfo]:
    """Вытащить из текста все строки подходящих форматов (с дедупликацией)."""
    found: dict[str, ProxyInfo] = {}
    for raw in _maybe_base64_decode(text).splitlines():
        info = parse_proxy_line(raw, source=source)
        if info is not None:
            found.setdefault(info.cache_line(), info)
    return list(found.values())