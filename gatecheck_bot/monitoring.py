"""Слежение за маршрутом (M3) + общая логика киллов — v0.10 (спека docs/MESSAGES.md).

Один маршрут на чат: /route A B (первый опрос + вердикт, §6), /route_status (§8),
/route_stop. Тик каждые ~50 с: для каждой системы маршрута — киллы zK за час;
«на гейте» = zkb.locationID ∈ itemID гейтов (D4). Маршрутные гейты (🚩) ведут в
±1 систему по маршруту; ПУШИ — только по ним, сразу, КД нет (§0.10). Киллы на
прочих гейтах систем маршрута пуша не дают — поднимают вердикт до 🟡 (§7).

Атака (§1/§7): тип кемпа по zkb.solo (соло/коллективный/смешанный), состав кораблей —
типы кораблей АТАКУЮЩИХ (полные киллмейлы zK отдаёт сам — zkb.json не нужен).
Бомбы/бабблы — только ПРИЗНАКИ (надёжного алгоритма не существует, §7):
💣 — weapon_type_id атакующих из SDE-группы Smart Bomb (id 55, грузится с ESI);
🫧 — дикторы/HIC среди кораблей атакующих или пусковики ISL (11584) / WDFG (16279).

zK/ESI ходят напрямую (без прокси бота) — оба доступны из заблокированных сетей.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiohttp

from .render import (
    bold,
    esc,
    format_isk,
    kills_word,
    quote,
    route_footer,
)
from .storage import Storage

logger = logging.getLogger("gatecheck_bot.monitoring")

ZK_BASE = "https://zkillboard.com/api"
ESI = "https://esi.evetech.net/latest"
ZK_UA = "GatecheckBot/0.10 (+https://github.com/sstarostin1/Gatecheck_bot)"

POLL_INTERVAL = 50.0     # период опроса маршрута, с (D-план: 40–60 с)
TTL_SECONDS = 3600.0     # время жизни слежки, с (D6: 1 час)
WINDOW_SECONDS = 3600.0  # окно статистики zK, с (§0.7: только «за последний час»)
WINDOW_DELTA = 600.0     # скользящее окно дельты «за последние 10 минут» (§1)
REQUEST_GAP = 0.3        # пауза между запросами к zK (этикет)
SEEN_CAP = 20000         # потолок памяти дедупликации на слежку
CAPSULE_ID = 670         # капсула — не корабль (в составе атакующих не показывается)

# Признаки бабблов (§7): только маркер, не гарантия.
DICTOR_IDS = frozenset({22456, 22464, 22444, 22460})  # Sabre/Flycatcher/Eris/Heretic
HIC_IDS = frozenset({11993, 11999, 12015, 12011})     # Devoter/Onyx/Phobos/Broadsword
BUBBLE_LAUNCHER_IDS = frozenset({11584, 16279})       # ISL / WDFG
SMARTBOMB_GROUP_ID = 55                               # SDE-группа Smart Bomb
SMARTBOMB_FALLBACK_IDS = frozenset({17926, 17938, 17930, 28211})

Fetcher = Callable[[aiohttp.ClientSession, str, int], Awaitable[list[dict] | None]]


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


async def fetch_system_kills(
    session: aiohttp.ClientSession, system_id: str, past_seconds: int
) -> list[dict] | None:
    """Киллы системы из zK за окно (полные киллмейлы); 429/403 — ретрай; сбой → None."""
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


_smartbomb_ids: frozenset[int] | None = None


async def smartbomb_type_ids(session: aiohttp.ClientSession) -> frozenset[int]:
    """ID смартбомб из SDE-группы 55 (ESI, кэш на процесс); ESI недоступен → дефолт (§7)."""
    global _smartbomb_ids
    if _smartbomb_ids is None:
        try:
            async with session.get(
                f"{ESI}/universe/groups/{SMARTBOMB_GROUP_ID}/",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    ids = frozenset(int(t) for t in (data.get("type_ids") or []))
                    if ids:
                        _smartbomb_ids = ids
        except Exception as exc:
            logger.debug("ESI group %s: %s: %s", SMARTBOMB_GROUP_ID, type(exc).__name__, exc)
        if _smartbomb_ids is None:
            _smartbomb_ids = SMARTBOMB_FALLBACK_IDS
    return _smartbomb_ids


def extract_features(kill: dict, bomb_ids: frozenset[int]) -> dict:
    """Признаки килла для блоков (§1/§7): атака, бомбы/бабблы, ISK, гейт."""
    zkb = kill.get("zkb", {})
    players = [a for a in kill.get("attackers", []) if a.get("character_id")]
    ships = sorted({int(a["ship_type_id"]) for a in players if a.get("ship_type_id")})
    weapons = {int(a["weapon_type_id"]) for a in players if a.get("weapon_type_id")}
    return {
        "ts": kill_epoch(kill),
        "kid": int(kill["killmail_id"]),
        "droppable": float(zkb.get("totalDroppableValue") or 0.0),
        "ship": _victim_ship(kill),
        "gate": int(zkb.get("locationID") or 0),
        "solo": bool(zkb.get("solo")),
        "npc": bool(zkb.get("npc")),
        "att_ships": tuple(ships[:20]),
        "bomb": any(w in bomb_ids for w in weapons),
        "bubble": (
            any(w in BUBBLE_LAUNCHER_IDS for w in weapons)
            or bool(set(ships) & (DICTOR_IDS | HIC_IDS))
        ),
    }


def attack_text(
    events: list[dict], ship_names: dict[int, str], include_attack: bool = True
) -> str | None:
    """Линия атаки по часовым событиям одного гейта (§1/§7) + признаки бомб/бабблов."""
    if not events:
        return None
    if all(e["npc"] for e in events):
        return "атака: NPC"
    non_npc = [e for e in events if not e["npc"]]
    solo = sum(1 for e in non_npc if e["solo"])
    group = len(non_npc) - solo
    if group == 0:
        camp = "соло"
    elif solo == 0:
        camp = "коллективный кемп"
    else:
        camp = "соло + групповые"
    line = f"атака: {camp}"
    if not include_attack:
        return line
    counts: dict[int, int] = {}
    for event in non_npc:
        for tid in event["att_ships"]:
            counts[tid] = counts.get(tid, 0) + 1
    if counts:
        listed = [
            f"{ship_names.get(tid, f'тип {tid}')}" + (f" ×{n}" if n > 1 else "")
            for tid, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        line += " · корабли: " + ", ".join(listed)
    markers: list[str] = []
    if any(e["bomb"] for e in events):
        markers.append("💣 смартбомбы")
    if any(e["bubble"] for e in events):
        markers.append("признаки бабблов")
    if markers:
        line += "; " + "; ".join(markers)
    return line


def gate_lines(
    events: list[dict],
    gate_id: int,
    now_epoch: float,
    dest_label: str,
    ship_names: dict[int, str],
    include_attack: bool = True,
    include_isk: bool = True,
    route_marker: bool = False,
) -> list[str]:
    """Строки одного гейта (§1/§7): счёт за час, дельта за 10 мин, ISK, атака."""
    hour = [
        e for e in events if e["ts"] >= now_epoch - WINDOW_SECONDS and e["gate"] == gate_id
    ]
    delta = sum(1 for e in hour if e["ts"] >= now_epoch - WINDOW_DELTA)
    line = f"{'🚩 ' if route_marker else ''}на {esc(dest_label)}: {len(hour)} за час"
    if delta:
        line += f" (+{delta} за последние 10 минут)"
    if include_isk:
        line += f", droppable {format_isk(sum(e['droppable'] for e in hour))}"
    lines = [line]
    attack = attack_text(hour, ship_names, include_attack)
    if attack:
        lines.append(attack)
    return lines


@dataclass
class RouteWatch:
    """Активная слежка одного чата за одним маршрутом (v0.10)."""

    chat_id: int
    route: list[str]  # system_id по порядку маршрута
    names: dict[str, str]  # system_id -> имя
    sec: dict[str, float]  # system_id -> security_status (статика, для <pre>-списка §6)
    gates_by_system: dict[str, set[int]]  # system_id -> itemID всех гейтов
    route_gates: dict[str, set[int]]  # system_id -> itemID маршрутных гейтов (🚩, §7)
    gate_names: dict[int, str]  # gate itemID -> имя ("Stargate (X)")
    started_at: float
    expires_at: float
    events: dict[str, deque] = field(default_factory=dict)  # system_id -> deque[dict]
    seen: set[int] = field(default_factory=set)  # killmail_id уже учтённых киллов
    baselined: bool = False  # первый тик/опрос фиксирует базу без алертов

    def __post_init__(self) -> None:
        for sid in self.route:
            self.events.setdefault(sid, deque())

    def remaining_minutes(self) -> int:
        return max(0, int((self.expires_at - time.monotonic()) / 60))

    def prune(self, now_epoch: float) -> None:
        """Выбросить события старше окна часа (+ запас)."""
        horizon = now_epoch - WINDOW_SECONDS - 60.0
        for events in self.events.values():
            while events and events[0]["ts"] < horizon:
                events.popleft()

    def hour_events(self, sid: str, now_epoch: float) -> list[dict]:
        return [e for e in self.events.get(sid, ()) if e["ts"] >= now_epoch - WINDOW_SECONDS]

    def ttl_line(self) -> str:
        first, last = self.names.get(self.route[0], "?"), self.names.get(self.route[-1], "?")
        return (
            f"🛡 Слежение маршрута {esc(first)} → {esc(last)} — "
            f"осталось {self.remaining_minutes()} мин."
        )


def route_verdict(watch: RouteWatch, now_epoch: float) -> tuple[int, dict[int, list[str]]]:
    """Уровень вердикта (§6): 3 очень опасен / 2 опасен / 1 под вопросом / 0 безопасен."""
    levels: dict[int, list[str]] = {3: [], 2: [], 1: [], 0: []}
    for sid in watch.route:
        hour = watch.hour_events(sid, now_epoch)
        route_n = sum(1 for e in hour if e["gate"] in watch.route_gates.get(sid, ()))
        marked = any(e["bomb"] or e["bubble"] for e in hour)
        name = watch.names[sid]
        if route_n and marked:
            levels[3].append(name)
        elif route_n:
            levels[2].append(name)
        elif len(hour) > route_n:
            levels[1].append(name)
    for level in (3, 2, 1):
        if levels[level]:
            return level, levels
    return 0, levels


def verdict_line(level: int, levels: dict[int, list[str]], prefix: bool = False) -> str:
    """Строка вердикта (§6/§8); prefix=True добавляет «Вердикт: » (только /route_status)."""
    lead = "Вердикт: " if prefix else ""
    if level == 3:
        return (
            f"🔴 <b>{lead}Маршрут очень опасен</b> "
            f"(кемпы на маршрутных гейтах: {esc(', '.join(levels[3]))}; бомбы/бабблы)"
        )
    if level == 2:
        return (
            f"🟠 <b>{lead}Маршрут опасен</b> "
            f"(кемпы на маршрутных гейтах: {esc(', '.join(levels[2]))})"
        )
    if level == 1:
        return (
            f"🟡 <b>{lead}Безопасность под вопросом</b> "
            f"({esc(', '.join(levels[1]))} — но не на маршрутных гейтах)"
        )
    return f"🟢 <b>{lead}Маршрут безопасен.</b>"


def route_system_block(
    watch: RouteWatch, sid: str, now_epoch: float, ship_names: dict[int, str]
) -> str:
    """Quote-блок горящей системы маршрута (§7): маршрутные гейты (🚩) первыми."""
    events = watch.hour_events(sid, now_epoch)
    idx = watch.route.index(sid)
    neighbours = [
        watch.names[watch.route[j]] for j in (idx - 1, idx + 1) if 0 <= j < len(watch.route)
    ]
    between = esc(" и ".join(neighbours)) if neighbours else "—"
    total = len(events)
    lines = [
        bold(f"В {esc(watch.names[sid])} между {between}") + f" {total} {kills_word(total)} на гейтах"
    ]
    gates: dict[int, list[dict]] = {}
    for event in events:
        gates.setdefault(event["gate"], []).append(event)
    route_gates = watch.route_gates.get(sid, set())
    ordered = sorted(
        gates.items(),
        key=lambda kv: (kv[0] not in route_gates, -len(kv[1])),  # 🚩 первыми (§7)
    )
    for gid, gate_events in ordered:
        dest = watch.gate_names.get(gid, "")
        dest_label = dest[len("Stargate ("):-1] if dest.startswith("Stargate (") else dest
        lines.append("")
        lines.extend(
            gate_lines(
                gate_events,
                gid,
                now_epoch,
                dest_label or f"гейт {gid}",
                ship_names,
                include_attack=True,
                include_isk=False,  # сумма выпавшего на маршруте не показывается (§7)
                route_marker=gid in route_gates,
            )
        )
    return quote("\n".join(lines))


class RouteMonitor:
    """Реестр активных слежек маршрутов + фоновый poller с алертами (§7, КД нет)."""

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
        self.storage = storage
        self.stat_requests = 0
        self.stat_errors = 0
        self.watches: dict[int, RouteWatch] = {}
        self._session: aiohttp.ClientSession | None = None
        self._ship_names: dict[int, str] = {}

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": ZK_UA})
        return self._session

    async def _resolve_ship_names(self, type_ids: set[int]) -> dict[int, str]:
        return await resolve_ship_names(self._ensure_session(), type_ids, self._ship_names)

    async def aclose(self) -> None:
        """Закрыть общую aiohttp-сессию (zK/ESI) при остановке процесса."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def begin(
        self,
        chat_id: int,
        route: list[str],
        graph,
        gate_index: dict[str, set[int]],
        gate_names: dict[int, str],
        gate_dest: dict[int, str],
    ) -> RouteWatch:
        """Создать слежку (новая заменяет прежнюю — один маршрут на чат) без первого опроса.

        Маршрутные гейты (🚩) вычисляются из dest_system_id гейтов: гейт маршрутный,
        если ведёт в предыдущую или следующую систему маршрута (§7).
        """
        route = [str(sid) for sid in route]
        route_gates: dict[str, set[int]] = {}
        for i, sid in enumerate(route):
            neighbours = {route[j] for j in (i - 1, i + 1) if 0 <= j < len(route)}
            route_gates[sid] = {
                gid
                for gid in gate_index.get(sid, set())
                if gate_dest.get(gid) in neighbours
            }
        watch = RouteWatch(
            chat_id=chat_id,
            route=route,
            names={sid: graph.name_of(sid) for sid in route},
            sec={
                sid: float((graph.systems.get(sid) or {}).get("security_status") or 0.0)
                for sid in route
            },
            gates_by_system={sid: set(gate_index.get(sid, ())) for sid in route},
            route_gates=route_gates,
            gate_names=dict(gate_names),
            started_at=time.monotonic(),
            expires_at=time.monotonic() + self.ttl_seconds,
        )
        self.watches[chat_id] = watch
        if self.storage is not None:
            self.storage.upsert_route(
                chat_id, route, time.time(), time.time() + self.ttl_seconds
            )
        return watch

    async def snapshot(self, watch: RouteWatch) -> bool:
        """Первый опрос (§6): наполнить события, базу зафиксировать — без алертов.

        Возвращает True, если по всем системам данные получены.
        """
        ok = await self._tick_watch(watch, bot=None, send=False)
        return ok

    async def force_tick(self, chat_id: int, bot=None, send: bool = True) -> None:
        """Немедленный тик слежки чата вне расписания."""
        watch = self.watches.get(chat_id)
        if watch is not None:
            await self._tick_watch(watch, bot=bot, send=send)

    async def status(self, chat_id: int) -> str:
        """Статус маршрута (§8): вердикт + блоки горящих систем / «не кемпят»."""
        watch = self.watches.get(chat_id)
        if watch is None:
            return "Слежение не активно. /route Amamake Jita — включить."
        now_epoch = datetime.now(UTC).timestamp()
        level, levels = route_verdict(watch, now_epoch)
        lines = [watch.ttl_line(), "", verdict_line(level, levels, prefix=True)]
        if level == 0:
            lines += [
                "",
                bold("Сейчас гайки на маршруте не кемпят."),
                f"Точек проверено: {len(watch.route)}.",
            ]
        else:
            ship_ids: set[int] = set()
            burning = []
            for sid in watch.route:
                if watch.hour_events(sid, now_epoch):
                    burning.append(sid)
                    ship_ids.update(
                        tid
                        for e in watch.hour_events(sid, now_epoch)
                        for tid in e["att_ships"]
                    )
            ship_names = await self._resolve_ship_names(ship_ids)
            lines += ["", bold("Сейчас гайки кемпят на маршруте:"), ""]
            lines += [route_system_block(watch, sid, now_epoch, ship_names) for sid in burning]
            lines.append(f"✅ Чисто: {len(watch.route) - len(burning)}/{len(watch.route)} систем")
        lines.append("")
        lines.append(route_footer())
        return "\n".join(lines)


    async def _fetch_kills(self, sid: str, window: int) -> list[dict] | None:
        """Обёртка fetcher'а со счётчиками запросов/ошибок (для /ping)."""
        self.stat_requests += 1
        kills = await self.fetcher(self._session, sid, window)
        if kills is None:
            self.stat_errors += 1
        return kills

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
                    "⏹ Слежение маршрута завершено — TTL "
                    f"{int(self.ttl_seconds / 60)} мин истёк.\n"
                    "/route Amamake Jita — включить заново.",
                )
            except Exception as exc:
                logger.warning("Не отправил TTL-сообщение в %s: %s", chat_id, exc)
        if self.storage is not None:
            self.storage.prune_kill_events(time.time() - 7 * 86400)
        for watch in list(self.watches.values()):
            await self._tick_watch(watch, bot)

    def stop(self, chat_id: int) -> str:
        """Остановить слежку чата (/route_stop)."""
        if self.watches.pop(chat_id, None) is None:
            return "Слежение не активно. /route Amamake Jita — включить."
        if self.storage is not None:
            self.storage.del_route(chat_id)
        return "🛡 Слежение остановлено."


    async def _tick_watch(self, watch: RouteWatch, bot=None, send: bool = True) -> bool:
        """Опрос всех систем маршрута за час; алерты — только по 🚩-гейтам (§7).

        Возвращает True, если по всем системам данные получены (сбой zK → False).
        """
        self._ensure_session()
        now_epoch = datetime.now(UTC).timestamp()
        bomb_ids = await smartbomb_type_ids(self._session)
        was_baselined = watch.baselined
        all_ok = True
        route_alert_sids: list[str] = []
        for sid in watch.route:
            kills = await self._fetch_kills(sid, self.window_seconds)
            if kills is None:
                all_ok = False
                kills = []
            gate_kills = filter_gate_kills(kills, watch.gates_by_system.get(sid, set()))
            for kill in gate_kills:
                kid = int(kill["killmail_id"])
                if kid in watch.seen:
                    continue
                feature = extract_features(kill, bomb_ids)
                watch.seen.add(kid)
                watch.events[sid].append(feature)
                if (
                    was_baselined
                    and feature["gate"] in watch.route_gates.get(sid, set())
                    and sid not in route_alert_sids
                ):
                    route_alert_sids.append(sid)
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
            if len(watch.seen) > SEEN_CAP:
                watch.seen = set(sorted(watch.seen)[-SEEN_CAP // 2 :])
            if self.request_gap:
                await asyncio.sleep(self.request_gap)
        watch.baselined = True
        if send and was_baselined and route_alert_sids and bot is not None:
            message = await self._alert_message(watch, route_alert_sids, now_epoch)
            try:
                await bot.send_message(watch.chat_id, message)
            except Exception as exc:
                logger.warning("Не отправил алерт маршрута в %s: %s", watch.chat_id, exc)
            if self.storage is not None:
                for sid in route_alert_sids:
                    self.storage.log_alert(watch.chat_id, "route", sid, time.time())
        return all_ok


    async def _alert_message(
        self, watch: RouteWatch, sids: list[str], now_epoch: float
    ) -> str:
        """Сообщение алерта маршрута (§7): блоки горящих систем + TTL + футер."""
        ship_ids: set[int] = set()
        for sid in sids:
            for event in watch.hour_events(sid, now_epoch):
                ship_ids.update(event["att_ships"])
        ship_names = await self._resolve_ship_names(ship_ids)
        total = len(watch.route)
        parts: list[str] = []
        for sid in sorted(sids, key=watch.route.index):
            pos = watch.route.index(sid) + 1
            header = bold(f"🚨 Кемп на маршруте (точка {pos}/{total}):")
            parts.append(header + "\n" + route_system_block(watch, sid, now_epoch, ship_names))
        return "\n\n".join(parts) + "\n\n" + watch.ttl_line() + "\n" + route_footer()

    def restore(
        self,
        graph,
        gate_index: dict[str, set[int]],
        gate_names: dict[int, str],
        gate_dest: dict[int, str],
    ) -> int:
        """Восстановить слежки маршрутов из Storage (после рестарта). Возвращает число."""
        if self.storage is None:
            return 0
        now_epoch = time.time()
        restored = 0
        for item in self.storage.active_routes(now_epoch):
            watch = self.begin(
                item["chat_id"], item["route"], graph, gate_index, gate_names, gate_dest
            )
            watch.baselined = True  # база была зафиксирована до рестарта
            # События и seen — из kill-кэша (с признаками): дублей не будет.
            for event in self.storage.kill_events_since(now_epoch - self.window_seconds):
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
            restored += 1
        return restored
