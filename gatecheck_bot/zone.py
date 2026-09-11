"""Фоновый мониторинг зоны фарма (M2): /zone on|off|status, всплески по D5.

Зона v1 — пресет «Hed + примыкающие системы» (D7): констелляция Hed (6 систем)
плюс системы, соединённые с ними гейтами (вычисляются по графу, §6.1).
Тик раз в ~4 мин (G1: 3–5 мин): для каждой системы — киллы zK за час, фильтр
«на гейте» = zkb.locationID ∈ itemID гейтов (D4). Скользящие окна считаются по
killmail_time киллов: всплеск ≥3/10 мин, накопление ≥8/час, droppable ISK
(zkb.totalDroppableValue — OQ-11) за 10 мин выше порога. Cooldown 15 мин на
систему, чтобы не спамить. Алерт — система, счётчики, ссылка на zKillboard.

zK/ESI ходят напрямую (без прокси бота) — оба доступны из заблокированных сетей.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiohttp

from .monitoring import (
    CAPSULE_ID,
    ZK_UA,
    Fetcher,
    fetch_system_kills,
    filter_gate_kills,
    format_isk,
    kill_epoch,
    resolve_ship_names,
)
from .storage import Storage

logger = logging.getLogger("gatecheck_bot.zone")

POLL_INTERVAL = 240.0   # тик зоны, с (G1: раз в 3–5 минут)
REQUEST_GAP = 0.3       # пауза между запросами к zK (этикет)
WINDOW_BURST = 600.0    # окно всплеска, с (D5: 10 минут)
WINDOW_HOUR = 3600.0    # окно накопления, с (D5: 1 час)
BURST_THRESHOLD = 3     # D5: ≥3 килла на гейтах системы за 10 минут
HOUR_THRESHOLD = 8      # D5: ≥8 киллов на гейтах системы за час
ISK_BURST = 100e6       # D5: droppable ISK за 10 мин выше порога (настраиваемый, дефолт 100M)
COOLDOWN = 900.0        # подавление повторных алертов системы, с (15 мин)

# Констелляция Hed (VISION §6.1): Amamake, Vard, Siseide, Lantorn, Dal, Auga.
HED_SYSTEM_IDS = frozenset(
    {"30002537", "30002538", "30002539", "30002540", "30002541", "30002542"}
)


def build_zone_preset(graph, hed_ids: frozenset[str] = HED_SYSTEM_IDS) -> list[str]:
    """Пресет «Hed + примыкающие системы»: Hed + системы, соединённые гейтами (§6.1)."""
    systems = set(hed_ids)
    for sid in hed_ids:
        systems.update(graph.adjacency.get(sid, ()))
    return sorted(systems & set(graph.systems))


def _kill_droppable(kill: dict) -> float:
    """Droppable ISK килла — готовое поле zKillboard (OQ-11)."""
    return float(kill.get("zkb", {}).get("totalDroppableValue") or 0.0)


SETTING_BOUNDS: dict[str, tuple[float, float]] = {
    "burst": (2, 10),          # всплеск: киллов на гейтах за 10 мин
    "hour": (4, 20),           # накопление: киллов за час
    "isk_burst": (10e6, 1e9),  # droppable ISK за 10 мин
    "cooldown": (300, 3600),   # пауза между алертами системы, с
}


def clamp_setting(key: str, value: float) -> float:
    """Ограничить значение порога разрешёнными границами (D5, OQ-8)."""
    low, high = SETTING_BOUNDS.get(key, (0.0, float("inf")))
    return min(max(value, low), high)


def parse_setting_value(key: str, raw: str) -> float | None:
    """/settings burst 4 · isk 200M · cooldown 20m → float (сек/ISK). None — не разобрать."""
    match = re.match(r"^(\d+(?:[.,]\d+)?)\s*(мин|h|ч|K|M|B|k|m|b)?$", raw.strip())
    if match is None:
        return None
    number = float(match.group(1).replace(",", "."))
    suffix = match.group(2) or ""
    if key == "isk_burst":
        return number * {"K": 1e3, "M": 1e6, "B": 1e9}.get(suffix.upper(), 1.0)
    if key == "cooldown":
        if suffix.lower() in {"h", "ч"}:
            return number * 3600
        return number * 60  # «20», «20мин», «20m» — минутами
    return number  # burst/hour — целые киллы


@dataclass
class ZoneWatch:
    """Активная подписка одного чата на зону (пороги — на слежку, для /settings)."""

    chat_id: int
    systems: list[str]  # system_id зоны (пресет)
    names: dict[str, str]  # system_id -> имя
    gates_by_system: dict[str, set[int]]  # system_id -> itemID гейтов
    gate_names: dict[int, str]  # gate itemID -> имя
    burst_threshold: int = BURST_THRESHOLD
    hour_threshold: int = HOUR_THRESHOLD
    isk_burst: float = ISK_BURST
    cooldown: float = COOLDOWN
    events: dict[str, deque] = field(default_factory=dict)  # system_id -> deque[dict]
    seen: set[int] = field(default_factory=set)  # killmail_id
    last_alert: dict[str, float] = field(default_factory=dict)  # system_id -> monotonic

    def __post_init__(self) -> None:
        for sid in self.systems:
            self.events.setdefault(sid, deque())

    def prune(self, now_epoch: float) -> None:
        """Выбросить события старше окна накопления (+ запас)."""
        horizon = now_epoch - WINDOW_HOUR - 60.0
        for events in self.events.values():
            while events and events[0]["ts"] < horizon:
                events.popleft()

    def counts(self, sid: str, now_epoch: float) -> tuple[int, int, float]:
        """(киллов на гейтах за 10 мин, за час, droppable ISK за 10 мин)."""
        events = self.events.get(sid, ())
        burst = hour = 0
        isk = 0.0
        for event in events:
            if event["ts"] >= now_epoch - WINDOW_BURST:
                burst += 1
                isk += event["droppable"]
            if event["ts"] >= now_epoch - WINDOW_HOUR:
                hour += 1
        return burst, hour, isk


class ZoneMonitor:
    """Реестр подписок на зону + фоновый poller с алертами по D5."""

    def __init__(
        self,
        poll_interval: float = POLL_INTERVAL,
        burst_threshold: int = BURST_THRESHOLD,
        hour_threshold: int = HOUR_THRESHOLD,
        isk_burst: float = ISK_BURST,
        cooldown: float = COOLDOWN,
        request_gap: float = REQUEST_GAP,
        hed_ids: frozenset[str] = HED_SYSTEM_IDS,
        storage: Storage | None = None,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self.burst_threshold = burst_threshold
        self.hour_threshold = hour_threshold
        self.isk_burst = isk_burst
        self.cooldown = cooldown
        self.request_gap = request_gap
        self.hed_ids = hed_ids  # ядро пресета (D7: новые зоны = новые наборы hed_ids)
        self.storage = storage  # SQLite-персистентность (рестарт переживает)
        self.stat_requests = 0
        self.stat_errors = 0
        self.fetcher = fetcher or fetch_system_kills
        self.watches: dict[int, ZoneWatch] = {}
        self._session: aiohttp.ClientSession | None = None
        self._ship_names: dict[int, str] = {}

    async def aclose(self) -> None:
        """Закрыть общую aiohttp-сессию (zK/ESI) при остановке процесса."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _fetch_kills(self, sid: str, window: int) -> list[dict] | None:
        """Обёртка fetcher'а со счётчиками запросов/ошибок (для /ping)."""
        self.stat_requests += 1
        kills = await self.fetcher(self._session, sid, window)
        if kills is None:
            self.stat_errors += 1
        return kills

    # --- управление подписками (вызывается из хэндлеров) ------------------

    def start(
        self,
        chat_id: int,
        graph,
        gate_index: dict[str, set[int]],
        gate_names,
        thresholds: dict | None = None,
    ) -> ZoneWatch:
        """Включить зону (пресет «Hed + соседи»; пороги — пользовательские или дефолты)."""
        systems = build_zone_preset(graph, self.hed_ids)
        merged: dict = {
            "burst_threshold": self.burst_threshold,
            "hour_threshold": self.hour_threshold,
            "isk_burst": self.isk_burst,
            "cooldown": self.cooldown,
        }
        for key, value in (thresholds or {}).items():
            if key == "burst":
                merged["burst_threshold"] = int(value)
            elif key == "hour":
                merged["hour_threshold"] = int(value)
            elif key in {"isk_burst", "cooldown"}:
                merged[key] = float(value)
        watch = ZoneWatch(
            chat_id=chat_id,
            systems=systems,
            names={sid: graph.name_of(sid) for sid in systems},
            gates_by_system={sid: set(gate_index.get(sid, ())) for sid in systems},
            gate_names=dict(gate_names),
            **merged,
        )
        self.watches[chat_id] = watch
        if self.storage is not None:
            self.storage.add_zone_sub(chat_id)
        return watch

    def stop(self, chat_id: int) -> str:
        if self.watches.pop(chat_id, None) is None:
            return "Зона не была включена. /zone on — включить."
        if self.storage is not None:
            self.storage.del_zone_sub(chat_id)
        return "🛡 Зона выключена."

    def status(self, chat_id: int) -> str:
        watch = self.watches.get(chat_id)
        if watch is None:
            return (
                "Зона не включена. /zone on — включить (пресет «Hed + соседи»); "
                "/zone off — выключить; /zone status — состояние."
            )
        now_epoch = datetime.now(UTC).timestamp()
        lines = [
            (
                f"🛡 Зона «Hed + соседи» — {len(watch.systems)} систем, "
                f"опрос каждые {int(self.poll_interval)} с."
            )
        ]
        clean = 0
        for sid in watch.systems:
            burst, hour, _isk = watch.counts(sid, now_epoch)
            if burst == 0 and hour == 0:
                clean += 1
                continue
            lines.append(f"⚠️ {watch.names[sid]}: за 10 мин {burst}, за час {hour}")
        lines.append(f"✅ Чисто: {clean} систем")
        lines.append("Выключить: /zone off")
        return "\n".join(lines)

    # --- фоновый цикл -----------------------------------------------------

    async def run_forever(self, bot) -> None:
        logger.info("Монитор зоны запущен (тик каждые %.0f с).", self.poll_interval)
        while True:
            try:
                await self._tick_all(bot)
            except Exception as exc:
                logger.warning("Тик зоны упал: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(self.poll_interval)

    async def _tick_all(self, bot) -> None:
        for watch in list(self.watches.values()):
            await self._tick_watch(watch, bot)

    async def _tick_watch(self, watch: ZoneWatch, bot) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": ZK_UA})
        now_epoch = datetime.now(UTC).timestamp()
        if self.storage is not None:
            self.storage.prune_kill_events(now_epoch - 7 * 86400)
        alerts: list[str] = []
        for sid in watch.systems:
            alert = await self._tick_system(watch, sid, now_epoch)
            if alert is not None:
                alerts.append(alert)
        if alerts:
            try:
                await bot.send_message(watch.chat_id, "\n\n".join(alerts))
            except Exception as exc:
                logger.warning("Не отправил алерт зоны в %s: %s", watch.chat_id, exc)

    async def _tick_system(self, watch: ZoneWatch, sid: str, now_epoch: float) -> str | None:
        """Опросить систему, обновить события; вернуть текст алерта (D5) или None."""
        kills = await self._fetch_kills(sid, int(WINDOW_HOUR)) or []
        gate_kills = filter_gate_kills(kills, watch.gates_by_system.get(sid, set()))
        for kill in gate_kills:
            kid = int(kill["killmail_id"])
            if kid in watch.seen:
                continue
            watch.seen.add(kid)
            event = {
                "ts": kill_epoch(kill),
                "kid": kid,
                "droppable": _kill_droppable(kill),
                "ship": int(kill.get("victim", {}).get("ship_type_id") or 0),
                "gate": int(kill.get("zkb", {}).get("locationID") or 0),
            }
            watch.events[sid].append(event)
            if self.storage is not None:
                self.storage.add_kill_event(
                    sid,
                    kid,
                    event["gate"],
                    event["ts"],
                    event["droppable"],
                    event["ship"] or None,
                )
        events = watch.events[sid]
        horizon = now_epoch - WINDOW_HOUR - 60.0
        while events and events[0]["ts"] < horizon:
            events.popleft()

        recent = [e for e in events if e["ts"] >= now_epoch - WINDOW_BURST]
        reasons: list[str] = []
        if len(recent) >= watch.burst_threshold:
            reasons.append(
                f"всплеск ≥{watch.burst_threshold}/10 мин ({len(recent)} киллов на гейтах)"
            )
        if len(events) >= watch.hour_threshold:
            reasons.append(f"накопление ≥{watch.hour_threshold}/час ({len(events)})")
        isk_10m = sum(e["droppable"] for e in recent)
        if isk_10m >= watch.isk_burst:
            reasons.append(f"droppable ISK за 10 мин ≥ {format_isk(watch.isk_burst)}")
        if not reasons:
            return None
        if time.monotonic() - watch.last_alert.get(sid, 0.0) < watch.cooldown:
            return None
        watch.last_alert[sid] = time.monotonic()

        by_gate: dict[int, list[dict]] = {}
        for event in recent:
            by_gate.setdefault(event["gate"], []).append(event)
        ship_ids = {event["ship"] for event in recent if event["ship"]} - {CAPSULE_ID}
        ship_names = await resolve_ship_names(self._session, ship_ids, self._ship_names)
        lines = [f"🔥 Зона фарма: {watch.names[sid]}", "Причина: " + "; ".join(reasons)]
        lines.append(f"Droppable ISK за 10 мин: {format_isk(isk_10m)}")
        for gid, gate_events in by_gate.items():
            gate_label = watch.gate_names.get(gid) or f"гейт {gid}"
            ships = [e for e in gate_events if e["ship"] != CAPSULE_ID]
            pods = len(gate_events) - len(ships)  # группировка ship+pod (OQ-4)
            parts = [f"×{len(ships)}"]
            if pods:
                parts.append(f"×{pods} капсул(ы)")
            lines.append(f"• {gate_label}: {', '.join(parts)}")
        if ship_names:
            lines.append("Корабли: " + ", ".join(sorted(ship_names.values())))
        lines.append(f"zKillboard: https://zkillboard.com/system/{sid}/")
        if self.storage is not None:
            self.storage.log_alert(watch.chat_id, "zone", sid, time.time())
        return "\n".join(lines)

    def restore(self, graph, gate_index: dict[str, set[int]], gate_names) -> int:
        """Восстановить подписки зоны из Storage (после рестарта). Возвращает число."""
        if self.storage is None:
            return 0
        restored = 0
        last_alerts = self.storage.last_alert_epochs("zone")
        for chat_id in self.storage.zone_sub_ids():
            if chat_id in self.watches:
                continue
            watch = self.start(chat_id, graph, gate_index, gate_names)
            for (cid, sid), epoch in last_alerts.items():
                if cid == chat_id:
                    # cooldown из лога: смещаем отсчёт из wallclock в monotonic
                    watch.last_alert[sid] = time.monotonic() - max(0.0, time.time() - epoch)
            restored += 1
        return restored
