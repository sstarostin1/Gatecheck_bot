"""Автономный пул: источники → живая проверка → кэш → ротация.

Философия устойчивости на длинных аптаймах:
- пул проверяется реальным запросом к api.telegram.org (а не «TCP открыт»);
- рабочие прокси сохраняются в кэш — следующий старт мгновенный, даже если
  источники недоступны;
- ротация ленивая: пока текущий прокси жив — ничего не трогаем; умер — вотчдог
  это замечает, берётся следующий; пул исчерпан — добыча запускается заново.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .checker import check_proxy
from .proxy_types import KIND_PRIORITY, VLESS, ProxyInfo, parse_proxy_line
from .sources import DEFAULT_SOURCES, extract_candidates, fetch_text
from .xray import XrayManager

logger = logging.getLogger("gatecheck_bot.transport")

CHECK_CONCURRENCY = 20  # размер батча одновременных проб
MIN_START_WORKING = 5   # ранний выход: достаточно для старта + пара фоллбеков


class ProxyPool:
    """Добыча, проверка, кэширование и ротация прокси для Bot API."""

    def __init__(
        self,
        sources: Iterable[str] = DEFAULT_SOURCES,
        manual: Iterable[str] = (),
        cache_path: str | Path = "data/proxy_cache.json",
        pool_size: int = 20,
        check_timeout: float = 8.0,
        xray: XrayManager | None = None,
    ) -> None:
        self.sources = tuple(sources) or DEFAULT_SOURCES
        self.manual = tuple(manual)
        self.cache_path = Path(cache_path)
        self.pool_size = max(1, int(pool_size))
        self.check_timeout = float(check_timeout)
        self.xray = xray or XrayManager()
        self.pool: list[ProxyInfo] = []
        self.index = -1
        self._vless_lock = asyncio.Lock()  # xray один — vless-пробы строго по очереди

    # --- кэш ------------------------------------------------------------

    def _load_cache_lines(self) -> list[str]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            lines = data.get("proxies", []) if isinstance(data, dict) else data
            return [str(line) for line in lines if str(line).strip()]
        except Exception:
            return []

    def _save_cache(self, working: list[ProxyInfo]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                "proxies": [p.cache_line() for p in working[:100]],
            }
            self.cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Не удалось сохранить кэш прокси: %s", exc)

    # --- добыча и проверка ----------------------------------------------

    async def _gather_candidates(self) -> dict[str, ProxyInfo]:
        """Кэш + manual + источники, с дедупликацией (кэш/manual приоритетнее)."""
        candidates: dict[str, ProxyInfo] = {}

        def _add(info: ProxyInfo) -> None:
            candidates.setdefault(info.cache_line(), info)

        for line in self._load_cache_lines():
            info = parse_proxy_line(line, source="cache")
            if info:
                _add(info)
        for line in self.manual:
            info = parse_proxy_line(line, source="manual")
            if info:
                _add(info)
        for url in self.sources:
            text = await fetch_text(url)
            if not text:
                continue
            found = extract_candidates(text, source=url)
            logger.info("Источник %s: %d кандидатов", url, len(found))
            for info in found:
                _add(info)
        return candidates

    async def _checked(self, info: ProxyInfo) -> ProxyInfo | None:
        latency = await check_proxy(info, self.check_timeout, self.xray)
        if latency is None:
            return None
        info.latency = latency
        logger.debug("OK %s (%.0f ms)", info.label(), latency * 1000)
        return info

    async def _check_one(self, info: ProxyInfo) -> ProxyInfo | None:
        if info.kind == VLESS:
            async with self._vless_lock:  # xray один — пробы vless не распараллеливаем
                return await self._checked(info)
        return await self._checked(info)

    async def _check_all(self, candidates: list[ProxyInfo]) -> list[ProxyInfo]:
        """Батчи проб; ранний выход, когда набран полный пул рабочих."""
        working: list[ProxyInfo] = []
        total = len(candidates)
        logger.info(
            "Проверяю %d кандидатов (по %d параллельно, таймаут %.0f с)...",
            total, CHECK_CONCURRENCY, self.check_timeout,
        )
        for start in range(0, total, CHECK_CONCURRENCY):
            batch = candidates[start : start + CHECK_CONCURRENCY]
            results = await asyncio.gather(*(self._check_one(info) for info in batch))
            working.extend(res for res in results if res is not None)
            done = min(start + CHECK_CONCURRENCY, total)
            logger.info("  проверено %d/%d, рабочих %d", done, total, len(working))
            if len(working) >= min(self.pool_size, MIN_START_WORKING):
                logger.info(
                    "Набрано %d рабочих — хватает для старта, проверку останавливаю.",
                    len(working),
                )
                break
        return working

    # --- публичный API ---------------------------------------------------

    async def refresh(self) -> int:
        """Пересобрать пул с нуля. Возвращает число рабочих прокси."""
        logger.info("Обновляю пул прокси: кэш + manual + %d источника(ов).", len(self.sources))
        candidates = await self._gather_candidates()
        logger.info("Кандидатов после дедупликации: %d.", len(candidates))
        # manual и кэш проверяем первыми — быстрее собирается стартовый пул.
        ordered = sorted(
            candidates.values(),
            key=lambda p: (p.source != "manual", p.source != "cache"),
        )
        working = await self._check_all(ordered)
        working.sort(key=lambda p: (KIND_PRIORITY.get(p.kind, 9), p.latency))
        self.pool = working[: self.pool_size]
        self.index = -1
        self._save_cache(working)
        logger.info(
            "Пул готов: %d рабочих (vless %d, socks5/http %d) из %d кандидатов.",
            len(self.pool),
            sum(1 for p in self.pool if p.kind == VLESS),
            sum(1 for p in self.pool if p.kind != VLESS),
            len(ordered),
        )
        return len(self.pool)

    async def next(self) -> ProxyInfo | None:
        """Следующий прокси (round-robin); для vless — с поднятым xray."""
        for _ in range(len(self.pool)):
            self.index = (self.index + 1) % len(self.pool)
            info = self.pool[self.index]
            if info.kind == VLESS:
                port = await self.xray.start(info.uri)
                if port is None:
                    logger.warning("xray не поднялся для %s — пропускаю.", info.label())
                    continue
                info.local_port = port
            return info
        return None