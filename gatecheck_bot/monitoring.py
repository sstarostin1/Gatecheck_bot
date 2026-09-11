"""Слежение за безопасностью маршрута (M3): poller zK → алерты о гейт-кампах.

Одна слежка на чат: `/route A B` включает, `/route stop` выключает, TTL 1 ч (D6).
Каждый тик: для каждой системы маршрута — киллы за последние WINDOW секунд из zK;
фильтр «на гейте» = `zkb.locationID` ∈ itemID гейтов системы (OQ-2 закрыт, D4).
Новые killmail_id → алерт в чат. `/route status` — статистика с прогрессией ▲▼.

zK и ESI ходят НАПРЯМУЮ (без прокси бота): оба сервиса доступны из сетей
с блокировкой Telegram (проверено spike-01.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

from .storage import Storage

logger = logging.getLogger("gatecheck_bot.monitoring")

ZK_BASE = "https://zkillboard.com/api"
ESI = "https://esi.evetech.net/latest"
ZK_UA = "GatecheckBot/0.7 (+https://github.com/sstarostin1/Gatecheck_bot)"

POLL_INTERVAL = 50.0    # период опроса маршрута, с (D-план: 40–60 с)
TTL_SECONDS = 3600.0    # время жизни слежки, с (D6: 1 час)
WINDOW_SECONDS = 3600.0  # окно статистики zK, с
REQUEST_GAP = 0.3       # пауза между запросами к zK (этикет)
SEEN_CAP = 20000        # потолок памяти дедупликации на слежку
CAPSULE_ID = 670        # капсула — для группировки ship+pod (OQ-4)


def format_isk(isk: float) -> str:
    """ISK в человекочитаемый вид: 1.23B / 456.7M / 12.3K."""
    if isk >= 1e12:
        return f"{isk / 1e12:.2f}T"
    if isk >= 1e9:
        return f"{isk / 1e9:.2f}B"
    if isk >= 1e6:
        return f"{isk / 1e6:.2f}M"
    if isk >= 1e3:
        return f"{isk / 1e3:.1f}K"
    return f"{isk:.0f}"


async def fetch_system_kills(
    session: aiohttp.ClientSession, system_id: str, past_seconds: int
) -> list[dict] | None:
    """Киллы системы из zK за окно; 429/403 — один ретрай с паузой; сбой → None."""
    url = f"{ZK_BASE}/solarSystemID/{system_id}/pastSeconds/{past_seconds}/"
    for attempt in range(2):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in {429, 403} and attempt == 0:
                    logger.warning("zK %s: HTTP %s — пауза и ретрай", system_id, resp.status)
                    await asyncio.sleep(2.0)
                    continue
                if resp.status != 200:
                    logger.warning("zK %s: HTTP %s", system_id, resp.status)
                    return None
                data = await resp.json()
                return data if isinstance(data, list) else None
        except Exception as exc:
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            logger.warning("zK %s: %s: %s", system_id, type(exc).__name__, exc)
            return None
    return None


def kill_epoch(kill: dict) -> float:
    """killmail_time (ISO UTC) → epoch секунд; окна киллов считаются по времени килла."""
    raw = str(kill.get("killmail_time", ""))
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.0


def _victim_ship(kill: dict) -> int:
    """ship_type_id жертвы (0, если не определён)."""
    return int(kill.get("victim", {}).get("ship_type_id") or 0)


def filter_gate_kills(kills: list[dict], gate_ids: set[int]) -> list[dict]:
    """Оставить только киллы НА гейтах: zkb.locationID ∈ itemID гейтов (D4)."""
    if not gate_ids:
        return []
    return [k for k in kills if k.get("zkb", {}).get("locationID") in gate_ids]


@dataclass
class RouteWatch:
    """Активная слежка одного чата за одним маршрутом."""

    chat_id: int
    route: list[str]  # system_id по порядку маршрута
    names: dict[str, str]  # system_id -> имя
    gates_by_system: dict[str, set[int]]  # system_id -> itemID гейтов
    gate_names: dict[int, str]  # gate itemID -> имя гейта
    started_at: float
    expires_at: float
    seen: set[int] = field(default_factory=set)  # killmail_id уже учтённых киллов
    baselined: bool = False  # первый тик фиксирует базу без алертов
    counts: dict[str, int] = field(default_factory=dict)  # system_id -> киллов на гейтах/окно
    prev_counts: dict[str, int] = field(default_factory=dict)  # прошлый тик (прогрессия)
    isk: dict[str, float] = field(default_factory=dict)  # system_id -> ISK потерь на гейтах

    def remaining_minutes(self) -> int:
        return max(0, int((self.expires_at - time.monotonic()) / 60))


Fetcher = Callable[[aiohttp.ClientSession, str, int], Awaitable[list[dict] | None]]


async def resolve_ship_names(
    session: aiohttp.ClientSession, type_ids: set[int], cache: dict[int, str]
) -> dict[int, str]:
    """Имена кораблей через ESI /universe/names; кэш навсегда (id типов стабильны)."""
    result: dict[int, str] = {}
    missing = [tid for tid in type_ids if tid not in cache]
    if missing:
        try:
            async with session.post(
                f"{ESI}/universe/names/",
                json=missing[:1000],
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    for item in await resp.json():
                        cache[int(item["id"])] = str(item.get("name", ""))
        except Exception as exc:
            logger.debug("ESI names: %s: %s", type(exc).__name__, exc)
    for tid in type_ids:
        name = cache.get(tid)
        if name:
            result[tid] = name
    return result


class RouteMonitor:
    """Реестр активных слежек + фоновый poller с алертами о гейт-кампах."""

    def __init__(
        self,
        poll_interval: float = POLL_INTERVAL,
        ttl_seconds: float = TTL_SECONDS,
        window_seconds: int = WINDOW_SECONDS,
        request_gap: float = REQUEST_GAP,
        storage: Storage | None = None,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self.ttl_seconds = ttl_seconds
        self.window_seconds = window_seconds
        self.request_gap = request_gap
        self.fetcher = fetcher or fetch_system_kills
        self.storage = storage  # SQLite-персистентность (рестарт переживает)
        self.stat_requests = 0
        self.stat_errors = 0
        self.watches: dict[int, RouteWatch] = {}
        self._session: aiohttp.ClientSession | None = None
        self._ship_names: dict[int, str] = {}

    # --- управление слежками (вызывается из хэндлеров) -------------------

    def start(
        self,
        chat_id: int,
        route: list[str],
        graph,
        gate_index: dict[str, set[int]],
        gate_names: dict[int, str],
    ) -> RouteWatch:
        """Включить слежку (новая заменяет прежнюю — один маршрут на чат)."""
        watch = RouteWatch(
            chat_id=chat_id,
            route=[str(sid) for sid in route],
            names={sid: graph.name_of(sid) for sid in route},
            gates_by_system={sid: set(gate_index.get(sid, ())) for sid in route},
            gate_names=dict(gate_names),
            started_at=time.monotonic(),
            expires_at=time.monotonic() + self.ttl_seconds,
        )
        self.watches[chat_id] = watch
        if self.storage is not None:
            self.storage.upsert_route(chat_id, watch.route, time.time(), time.time() + self.ttl_seconds)
        return watch

    def stop(self, chat_id: int) -> str:
        if self.watches.pop(chat_id, None) is None:
            return "Слежение не было включено. /route A B — включить."
        if self.storage is not None:
            self.storage.del_route(chat_id)
        return "🛡 Слежение остановлено."

    async def aclose(self) -> None:
        """Закрыть общую aiohttp-сессию (zK/ESI) при остановке процесса."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def status(self, chat_id: int) -> str:
        watch = self.watches.get(chat_id)
        if watch is None:
            return (
                "Слежение не активно. Включи: /route A B\n"
                "(TTL 1 час; остановка: /route stop, статистика: /route status)"
            )
        lines = [f"🛡 Слежение маршрута — осталось {watch.remaining_minutes()} мин."]
        for sid in watch.route:
            count = watch.counts.get(sid)
            name = watch.names[sid]
            if count is None:
                lines.append(f"⏳ {name}: собираю данные...")
            elif count == 0:
                lines.append(f"✅ {name}: на гейтах чисто")
            else:
                delta = count - watch.prev_counts.get(sid, count)
                arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "•")
                delta_txt = "" if delta == 0 else f" {arrow}{abs(delta)}"
                lines.append(
                    f"⚠️ {name}: {count} килл(ов) на гейтах за час{delta_txt}, "
                    f"ISK {format_isk(watch.isk.get(sid, 0.0))}"
                )
        lines.append("Остановка: /route stop")
        return "\n".join(lines)

    # --- фоновый цикл -----------------------------------------------------

    async def run_forever(self, bot) -> None:
        logger.info("Монитор маршрутов запущен (тик каждые %.0f с).", self.poll_interval)
        while True:
            try:
                await self._tick_all(bot)
            except Exception as exc:
                logger.warning("Тик монитора упал: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(self.poll_interval)

    async def _tick_all(self, bot) -> None:
        now = time.monotonic()
        expired = [cid for cid, w in self.watches.items() if now >= w.expires_at]
        for chat_id in expired:
            self.watches.pop(chat_id, None)
            if self.storage is not None:
                self.storage.del_route(chat_id)
            try:
                await bot.send_message(
                    chat_id,
                    f"⏹ Слежение маршрута завершено — TTL {int(self.ttl_seconds / 60)} мин истёк.\n"
                    "/route A B — включить заново.",
                )
            except Exception as exc:
                logger.warning("Не отправил TTL-сообщение в %s: %s", chat_id, exc)
        if self.storage is not None:
            # Чистка kill-кэша старше 7 суток (VISION §8).
            self.storage.prune_kill_events(time.time() - 7 * 86400)
        for watch in list(self.watches.values()):
            await self._tick_watch(watch, bot)

    async def _tick_watch(self, watch: RouteWatch, bot) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": ZK_UA})
        watch.prev_counts = dict(watch.counts)
        alerts: list[str] = []
        alerted_sids: list[str] = []
        total = len(watch.route)
        for pos, sid in enumerate(watch.route, start=1):
            kills = await self._fetch_kills(sid, self.window_seconds) or []
            gate_kills = filter_gate_kills(kills, watch.gates_by_system.get(sid, set()))
            watch.counts[sid] = len(gate_kills)
            watch.isk[sid] = sum(
                float(k.get("zkb", {}).get("totalValue") or 0) for k in gate_kills
            )
            new_kills = [k for k in gate_kills if int(k["killmail_id"]) not in watch.seen]
            watch.seen.update(int(k["killmail_id"]) for k in gate_kills)
            if len(watch.seen) > SEEN_CAP:
                watch.seen = set(sorted(watch.seen)[-SEEN_CAP // 2 :])
            if self.storage is not None:
                for k in new_kills:
                    self.storage.add_kill_event(
                        sid,
                        int(k["killmail_id"]),
                        int(k.get("zkb", {}).get("locationID") or 0),
                        kill_epoch(k),
                        float(k.get("zkb", {}).get("totalDroppableValue") or 0.0),
                        _victim_ship(k) or None,
                    )
            if watch.baselined and new_kills:
                type_ids = {_victim_ship(k) for k in new_kills} - {0, CAPSULE_ID}
                ship_names = await self._resolve_ship_names(type_ids)
                alerts.append(
                    self._format_alert(watch, sid, pos, total, new_kills, gate_kills, ship_names)
                )
                alerted_sids.append(sid)
            if self.request_gap:
                await asyncio.sleep(self.request_gap)
        watch.baselined = True
        if alerts:
            try:
                await bot.send_message(watch.chat_id, "\n\n".join(alerts))
            except Exception as exc:
                logger.warning("Не отправил алерт в %s: %s", watch.chat_id, exc)
            if self.storage is not None:
                for sid in alerted_sids:
                    self.storage.log_alert(watch.chat_id, "route", sid, time.time())

    async def _resolve_ship_names(self, type_ids: set[int]) -> dict[int, str]:
        """Имена кораблей через ESI /universe/names; кэш навсегда (id стабильны)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": ZK_UA})
        return await resolve_ship_names(self._session, type_ids, self._ship_names)

    def _format_alert(
        self,
        watch: RouteWatch,
        sid: str,
        pos: int,
        total: int,
        new_kills: list[dict],
        gate_kills: list[dict],
        ship_names: dict[int, str],
    ) -> str:
        by_gate: dict[int, list[dict]] = {}
        for k in new_kills:
            by_gate.setdefault(int(k["zkb"]["locationID"]), []).append(k)
        hour_by_gate: dict[int, int] = {}
        for k in gate_kills:
            gid = int(k["zkb"]["locationID"])
            hour_by_gate[gid] = hour_by_gate.get(gid, 0) + 1
        lines = [f"🚨 Гейт-камп: {watch.names[sid]} (точка {pos}/{total} маршрута)"]
        for gid, kills in by_gate.items():
            gate_label = watch.gate_names.get(gid) or f"гейт {gid}"
            ships = [k for k in kills if _victim_ship(k) != CAPSULE_ID]
            pods = len(kills) - len(ships)  # группировка ship+pod (OQ-4)
            parts = [f"+{len(ships)} новых"]
            if pods:
                parts.append(f"+{pods} капсул(ы)")
            lines.append(
                f"• {gate_label}: {', '.join(parts)}, за час {hour_by_gate.get(gid, 0)}"
            )
        isk = sum(
            float(k.get("zkb", {}).get("totalDroppableValue") or 0) for k in gate_kills
        )
        lines.append(f"Droppable ISK на гейтах за час: {format_isk(isk)}")
        if ship_names:
            lines.append("Корабли: " + ", ".join(sorted(ship_names.values())))
        lines.append(f"zKillboard: https://zkillboard.com/system/{sid}/")
        return "\n".join(lines)

    async def _fetch_kills(self, sid: str, window: int) -> list[dict] | None:
        """Обёртка fetcher'а со счётчиками запросов/ошибок (для /ping)."""
        self.stat_requests += 1
        kills = await self.fetcher(self._session, sid, window)
        if kills is None:
            self.stat_errors += 1
        return kills

    async def fetch_hour_counts(
        self, route: list[str], gate_index: dict[str, set[int]]
    ) -> dict[str, int | None]:
        """Живые счётчики киллов на гейтах систем маршрута за час (§5, шаг 2)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": ZK_UA})
        semaphore = asyncio.Semaphore(8)

        async def _one(sid: str) -> tuple[str, int | None]:
            async with semaphore:
                kills = await self._fetch_kills(sid, 3600)
            if kills is None:
                return sid, None
            return sid, len(filter_gate_kills(kills, gate_index.get(sid, set())))

        results = await asyncio.gather(*(_one(sid) for sid in set(route)))
        return dict(results)

    def restore(self, graph, gate_index: dict[str, set[int]], gate_names) -> int:
        """Восстановить слежки маршрутов из Storage (после рестарта). Возвращает число."""
        if self.storage is None:
            return 0
        now_epoch = time.time()
        restored = 0
        for item in self.storage.active_routes(now_epoch):
            route = item["route"]
            gates_by_system = {sid: set(gate_index.get(sid, ())) for sid in route}
            watch = RouteWatch(
                chat_id=item["chat_id"],
                route=route,
                names={sid: graph.name_of(sid) for sid in route},
                gates_by_system=gates_by_system,
                gate_names=dict(gate_names),
                started_at=time.monotonic() - (now_epoch - item["started_epoch"]),
                expires_at=time.monotonic() + (item["expires_epoch"] - now_epoch),
                baselined=True,  # база уже зафиксирована до рестарта
            )
            for event in self.storage.kill_events_since(now_epoch - self.window_seconds):
                if (
                    event["system_id"] in gates_by_system
                    and event["gate_id"] in gates_by_system[event["system_id"]]
                ):
                    watch.seen.add(event["kill_id"])
            self.watches[watch.chat_id] = watch
            restored += 1
        return restored
