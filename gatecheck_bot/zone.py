"""Фоновый мониторинг зоны фарма (M2) — v0.10 (спека docs/MESSAGES.md).

Зона — пресет «Hed + соседи» (D7): констелляция Hed (6 систем) плюс системы,
соединённые с ними гейтами (§6.1 VISION). Тик раз в ~4 мин (G1): для каждой системы —
киллы zK за час, фильтр «на гейте» = zkb.locationID ∈ itemID гейтов (D4).

v0.10 (§0.7): десятиминутные триггеры УДАЛЕНЫ (zK отдаёт киллы с задержкой — окно
всегда пустое). Триггеры только часовые: ≥ {hour} киллов на гейтах системы за час
(дефолт 8) ИЛИ droppable ISK на гейтах за час ≥ {isk} (дефолт 0.5B). Анти-спам:
повторный алерт только при НОВЫХ киллах с прошлого алерта + cooldown 15 мин
(настраивается, только для зоны). Формат алертов/статусов — §1–§4: bold-заголовок
«Сейчас гайки кемпят в системе/системах:», quote-блоки систем (первая строка
«{система} N киллов и {ISK} на гейтах», пер-гейт дельта «(+N за последние 10 минут)»),
footer с EVE Time — курсивом. Блоки систем сортируются по убыванию киллов (§0.12).
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
    WINDOW_DELTA,
    ZK_UA,
    Fetcher,
    attack_text,
    extract_features,
    fetch_system_kills,
    filter_gate_kills,
    format_isk,
    kill_epoch,
    resolve_ship_names,
    smartbomb_type_ids,
)
from .render import bold, esc, italic, kills_word, quote, systems_word, zone_footer
from .storage import Storage

logger = logging.getLogger("gatecheck_bot.zone")

POLL_INTERVAL = 240.0   # тик зоны, с (G1: раз в 3–5 минут)
REQUEST_GAP = 0.3       # пауза между запросами к zK (этикет)
HOUR_THRESHOLD = 8      # D5: ≥8 киллов на гейтах системы за час
ISK_THRESHOLD = 5e8     # D5: droppable ISK за час ≥ 0.5B (настраиваемый)
COOLDOWN = 900.0        # подавление повторных алертов системы, с (15 мин)
SETTING_BOUNDS: dict[str, tuple[float, float]] = {
    "hour": (4, 20),          # киллов на гейтах за час
    "isk": (1e7, 5e9),        # droppable ISK за час (10M…5B)
    "cooldown": (300, 3600),  # пауза между алертами системы, с (5…60 мин)
}

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


def clamp_setting(key: str, value: float) -> float:
    """Ограничить значение порога разрешёнными границами (D5, OQ-8)."""
    low, high = SETTING_BOUNDS.get(key, (0.0, float("inf")))
    return min(max(value, low), high)


def parse_setting_value(key: str, raw: str) -> float | None:
    """/settings_hour 10 · /settings_isk 0.7B · /settings_cooldown 20м → float. None — ошибка."""
    match = re.match(r"^(\d+(?:[.,]\d+)?)\s*(мин|м|h|ч|K|M|B|k|m|b)?$", raw.strip())
    if match is None:
        return None
    number = float(match.group(1).replace(",", "."))
    suffix = match.group(2) or ""
    if key == "isk":
        return number * {"K": 1e3, "M": 1e6, "B": 1e9}.get(suffix.upper(), 1.0)
    if key == "cooldown":
        if suffix.lower() in {"h", "ч"}:
            return number * 3600
        return number * 60  # «20», «20мин», «20m» — минутами
    return number  # hour — целые киллы


def zone_system_block(
    name: str,
    events: list[dict],
    now_epoch: float,
    gate_labels: dict[int, str],
    ship_names: dict[int, str],
    include_attack: bool = True,
) -> str:
    """Quote-блок системы зоны (§1): первая строка + горящие гейты с дельтой и ISK.

    events — киллы «на гейтах» (D4) за последний час: {ts, kid, droppable, ship,
    gate, solo, npc, att_ships, bomb, bubble}.
    """
    hour = [e for e in events if e["ts"] >= now_epoch - 3600.0]
    total = len(hour)
    isk = sum(e["droppable"] for e in hour)
    lines = [f"{esc(name)} {total} {kills_word(total)} и {esc(format_isk(isk))} ISK на гейтах"]
    gates: dict[int, list[dict]] = {}
    for event in hour:
        gates.setdefault(event["gate"], []).append(event)
    for gid, gate_events in sorted(gates.items(), key=lambda kv: -len(kv[1])):
        label = gate_labels.get(gid) or f"гейт {gid}"
        line = f"на {esc(label)}: {len(gate_events)} за час"
        delta = sum(1 for e in gate_events if e["ts"] >= now_epoch - WINDOW_DELTA)
        if delta:
            line += f" (+{delta} за последние 10 минут)"
        line += f", droppable {esc(format_isk(sum(e['droppable'] for e in gate_events)))}"
        lines.append("")
        lines.append(line)
        attack = attack_text(gate_events, ship_names, include_attack)
        if attack:
            lines.append(esc(attack))
    return quote("\n".join(lines))


@dataclass
class ZoneWatch:
    """Активная подписка одного чата на зону (пороги — на слежку, для /settings)."""

    chat_id: int
    systems: list[str]  # system_id зоны (пресет)
    names: dict[str, str]  # system_id -> имя
    gates_by_system: dict[str, set[int]]  # system_id -> itemID гейтов
    gate_names: dict[int, str]  # gate itemID -> имя ("Stargate (X)")
    gate_labels: dict[int, str]  # gate itemID -> система-назначения («на Amamake», §1)
    hour_threshold: int = HOUR_THRESHOLD
    isk: float = ISK_THRESHOLD
    cooldown: float = COOLDOWN
    include_attack: bool = True  # /settings_attackers: состав и вид кемпа в алертах
    events: dict[str, deque] = field(default_factory=dict)  # system_id -> deque[dict]
    seen: set[int] = field(default_factory=set)  # killmail_id
    last_alert: dict[str, float] = field(default_factory=dict)  # system_id -> monotonic
    last_alert_ts: dict[str, float] = field(default_factory=dict)  # system_id -> kill epoch:
    # ts последнего килла, уже попавшего в алерт; «новые» = ts больше этого значения

    def __post_init__(self) -> None:
        for sid in self.systems:
            self.events.setdefault(sid, deque())

    def prune(self, now_epoch: float) -> None:
        """Выбросить события старше окна накопления (+ запас)."""
        horizon = now_epoch - 3600.0 - 60.0
        for events in self.events.values():
            while events and events[0]["ts"] < horizon:
                events.popleft()

    def counts(self, sid: str, now_epoch: float) -> tuple[int, float]:
        """(киллов на гейтах за час, droppable ISK за час) — триггеры D5 v2 (§0.7)."""
        events = [e for e in self.events.get(sid, ()) if e["ts"] >= now_epoch - 3600.0]
        return len(events), sum(e["droppable"] for e in events)


class ZoneMonitor:
    """Реестр подписок зоны + фоновый poller с алертами D5 (§1–§2)."""

    def __init__(
        self,
        poll_interval: float = POLL_INTERVAL,
        hour_threshold: int = HOUR_THRESHOLD,
        isk: float = ISK_THRESHOLD,
        cooldown: float = COOLDOWN,
        request_gap: float = REQUEST_GAP,
        hed_ids: frozenset[str] = HED_SYSTEM_IDS,
        storage: Storage | None = None,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self.hour_threshold = hour_threshold
        self.isk = isk
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

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": ZK_UA})
        return self._session

    async def _resolve_ship_names(self, type_ids: set[int]) -> dict[int, str]:
        return await resolve_ship_names(self._ensure_session(), type_ids, self._ship_names)

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
        gate_labels: dict[int, str],
        thresholds: dict | None = None,
    ) -> ZoneWatch:
        """Включить зону (пресет «Hed + соседи»; пороги — пользовательские или дефолты)."""
        systems = build_zone_preset(graph, self.hed_ids)
        merged: dict = {
            "hour_threshold": self.hour_threshold,
            "isk": self.isk,
            "cooldown": self.cooldown,
            "include_attack": True,
        }
        for key, value in (thresholds or {}).items():
            if key == "hour":
                merged["hour_threshold"] = int(value)
            elif key == "isk":
                merged["isk"] = float(value)
            elif key == "cooldown":
                merged["cooldown"] = float(value)
            elif key == "attackers":
                merged["include_attack"] = bool(value)
        watch = ZoneWatch(
            chat_id=chat_id,
            systems=systems,
            names={sid: graph.name_of(sid) for sid in systems},
            gates_by_system={sid: set(gate_index.get(sid, ())) for sid in systems},
            gate_names=dict(gate_names),
            gate_labels=dict(gate_labels),
            **merged,
        )
        self.watches[chat_id] = watch
        if self.storage is not None:
            self.storage.add_zone_sub(chat_id)
        return watch

    async def force_tick(self, chat_id: int, bot=None) -> None:
        """Немедленный тик зоны чата вне расписания (кнопка «🔄 Гейты: обновить»)."""
        watch = self.watches.get(chat_id)
        if watch is not None:
            await self._tick_watch(watch, bot)

    def stop(self, chat_id: int) -> str:
        if self.watches.pop(chat_id, None) is None:
            return "Зона не была включена. /zone_on — включить."
        if self.storage is not None:
            self.storage.del_zone_sub(chat_id)
        return "🛡 Зона выключена."


    async def status(self, chat_id: int) -> str:
        """Статус зоны из накопленных событий (§3): заголовок + блоки + чисто + футер."""
        watch = self.watches.get(chat_id)
        if watch is None:
            return (
                "Зона не включена. /zone_on — включить (пресет «Hed + соседи»); "
                "/zone_off — выключить; /zone_status — состояние."
            )
        now_epoch = datetime.now(UTC).timestamp()
        blocks: list[tuple[int, str]] = []
        clean = 0
        ship_ids: set[int] = set()
        for sid in watch.systems:
            hour = [e for e in watch.events.get(sid, ()) if e["ts"] >= now_epoch - 3600.0]
            if not hour:
                clean += 1
                continue
            ship_ids.update(tid for e in hour for tid in e["att_ships"])
            blocks.append((len(hour), sid))
        if not blocks:
            head = bold("Сейчас гайки в зоне не кемпят.")
        elif len(blocks) == 1:
            head = bold("Сейчас гайки кемпят в системе:")
        else:
            head = bold("Сейчас гайки кемпят в системах:")
        ship_names = await self._resolve_ship_names(ship_ids)
        lines = [head]
        for _, sid in sorted(blocks, key=lambda b: -b[0]):  # §0.12: горячие — первыми
            lines.append("")
            lines.append(
                zone_system_block(
                    watch.names[sid],
                    watch.events.get(sid, ()),
                    now_epoch,
                    watch.gate_labels,
                    ship_names,
                    watch.include_attack,
                )
            )
        total = len(watch.systems)
        lines.append(f"✅ Чисто: {clean}/{total} {systems_word(total)}")
        lines.append("")
        lines.append(zone_footer())
        return "\n".join(lines)


    async def live_snapshot(self, graph, gate_index, gate_names, gate_labels) -> str:
        """Разовый опрос зоны без подписки (§4): формат §3 + подсказка /zone_on."""
        self._ensure_session()
        systems = build_zone_preset(graph, self.hed_ids)
        now_epoch = datetime.now(UTC).timestamp()
        bomb_ids = await smartbomb_type_ids(self._session)
        blocks: list[tuple[int, str, list[dict]]] = []
        clean = 0
        for sid in systems:
            kills = await self._fetch_kills(sid, 3600) or []
            gate_kills = filter_gate_kills(kills, gate_index.get(sid, set()))
            hour = [
                extract_features(kill, bomb_ids)
                for kill in gate_kills
                if kill_epoch(kill) >= now_epoch - 3600.0
            ]
            if hour:
                blocks.append((len(hour), sid, hour))
            else:
                clean += 1
            if self.request_gap:
                await asyncio.sleep(self.request_gap)
        if not blocks:
            lines = [bold("Сейчас гайки в зоне не кемпят.")]
            lines.append(f"✅ Чисто: {clean} {systems_word(clean)}")
            lines.append(italic("Постоянное слежение: /zone_on"))
            return "\n".join(lines)
        ship_ids: set[int] = set()
        for _, _, hour in blocks:
            ship_ids.update(tid for e in hour for tid in e["att_ships"])
        ship_names = await resolve_ship_names(self._session, ship_ids, self._ship_names)
        lines = [bold("Сейчас гайки кемпят в системах:")]
        for _, sid, hour in sorted(blocks, key=lambda b: -b[0]):  # §0.12
            lines.append("")
            lines.append(
                zone_system_block(graph.name_of(sid), hour, now_epoch, gate_labels, ship_names)
            )
        lines.append(f"✅ Чисто: {clean}/{len(systems)} {systems_word(len(systems))}")
        lines.append("")
        lines.append(zone_footer())
        lines.append(italic("Постоянное слежение: /zone_on"))
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
        """Тик зоны: все системы, алерт по §1–§2 (блоки отсортированы по киллам)."""
        self._ensure_session()
        now_epoch = datetime.now(UTC).timestamp()
        if self.storage is not None:
            self.storage.prune_kill_events(now_epoch - 7 * 86400)
        blocks: list[tuple[int, str]] = []
        alerted_sids: list[str] = []
        for sid in watch.systems:
            block = await self._tick_system(watch, sid, now_epoch)
            if block is not None:
                blocks.append(block)
                alerted_sids.append(sid)
        if not blocks:
            return
        head = (
            bold("Сейчас гайки кемпят в системе:")
            if len(blocks) == 1
            else bold("Сейчас гайки кемпят в системах:")
        )
        lines = [head]
        for _, block_text in sorted(blocks, key=lambda b: -b[0]):  # §0.12
            lines.append("")
            lines.append(block_text)
        lines.append("")
        lines.append(zone_footer())
        try:
            await bot.send_message(watch.chat_id, "\n".join(lines))
        except Exception as exc:
            logger.warning("Не отправил алерт зоны в %s: %s", watch.chat_id, exc)
        if self.storage is not None:
            for sid in alerted_sids:
                self.storage.log_alert(watch.chat_id, "zone", sid, time.time())

    async def _tick_system(
        self, watch: ZoneWatch, sid: str, now_epoch: float
    ) -> tuple[int, str] | None:
        """Опросить систему, обновить события; вернуть (киллов за час, блок) или None.

        Алерт только если: сработал часовой триггер D5 v2 (§0.7) И появились киллы,
        которых не было в предыдущем алерте (анти-спам §0.9) И cooldown прошёл (§0.10).
        """
        kills = await self._fetch_kills(sid, 3600) or []
        gate_kills = filter_gate_kills(kills, watch.gates_by_system.get(sid, set()))
        bomb_ids = await smartbomb_type_ids(self._session)
        for kill in gate_kills:
            kid = int(kill["killmail_id"])
            if kid in watch.seen:
                continue
            feature = extract_features(kill, bomb_ids)
            watch.seen.add(kid)
            watch.events[sid].append(feature)
            if self.storage is not None:
                self.storage.add_kill_event(
                    sid,
                    kid,
                    feature["gate"],
                    feature["ts"],
                    feature["droppable"],
                    feature["ship"] or None,
                    features=feature,
                )
        watch.prune(now_epoch)
        hour_n, isk_hour = watch.counts(sid, now_epoch)
        triggered = hour_n >= watch.hour_threshold or isk_hour >= watch.isk
        last_ts = watch.last_alert_ts.get(sid, 0.0)
        hour_events = [e for e in watch.events[sid] if e["ts"] >= now_epoch - 3600.0]
        has_new = any(e["ts"] > last_ts for e in hour_events)
        if not triggered or not has_new:
            return None
        if time.monotonic() - watch.last_alert.get(sid, 0.0) < watch.cooldown:
            return None
        watch.last_alert[sid] = time.monotonic()
        watch.last_alert_ts[sid] = max((e["ts"] for e in hour_events), default=0.0)
        ship_ids = {tid for e in hour_events for tid in e["att_ships"]}
        ship_names = await self._resolve_ship_names(ship_ids)
        block = zone_system_block(
            watch.names[sid],
            hour_events,
            now_epoch,
            watch.gate_labels,
            ship_names,
            watch.include_attack,
        )
        return hour_n, block

    def restore(self, graph, gate_index, gate_names, gate_labels) -> int:
        """Восстановить подписки зоны из Storage (после рестарта). Возвращает число."""
        if self.storage is None:
            return 0
        restored = 0
        last_alerts = self.storage.last_alert_epochs("zone")
        for chat_id in self.storage.zone_sub_ids():
            if chat_id in self.watches:
                continue
            watch = self.start(chat_id, graph, gate_index, gate_names, gate_labels)
            # События — из kill-кэша (с признаками): /zone_status сразу информативен.
            for event in self.storage.kill_events_since(time.time() - 3600.0 - 60.0):
                sid = event["system_id"]
                if sid not in watch.gates_by_system:
                    continue
                if event["gate_id"] and event["gate_id"] not in watch.gates_by_system[sid]:
                    continue
                watch.seen.add(event["kill_id"])
                if event.get("features"):
                    feature = dict(event["features"])
                    feature.setdefault("ts", event["kill_ts"])
                    feature.setdefault("kid", event["kill_id"])
                    feature.setdefault("gate", event["gate_id"])
                    watch.events[sid].append(feature)
            for (cid, sid), epoch in last_alerts.items():
                if cid == chat_id:
                    # cooldown из лога: смещаем отсчёт из wallclock в monotonic
                    watch.last_alert[sid] = time.monotonic() - max(0.0, time.time() - epoch)
            restored += 1
        return restored
