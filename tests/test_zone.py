"""Тесты монитора зоны v0.10: часовые триггеры D5 v2, формат §1–§4, анти-спам, cooldown."""

import asyncio
from datetime import UTC, datetime, timedelta

import gatecheck_bot.zone as zone_module
from gatecheck_bot.routing import Graph
from gatecheck_bot.zone import ZoneMonitor, build_zone_preset


def make_graph() -> Graph:
    systems = {
        "1": {"name": "Alpha", "security_status": 0.5},
        "2": {"name": "Beta", "security_status": 0.4},
        "3": {"name": "Gamma", "security_status": 0.3},
        "4": {"name": "Delta", "security_status": 0.2},
        "5": {"name": "Faraway", "security_status": 0.1},  # не соединён с Hed-системами
    }
    adjacency = {"1": ["2", "3"], "2": ["1", "4"], "3": ["1"], "4": ["2"]}
    return Graph(systems=systems, adjacency=adjacency, meta={})


HED = frozenset({"1", "2"})


def kill(
    kid: int,
    loc: int,
    droppable: float = 1e6,
    ship: int = 587,
    ago: float = 60.0,
    solo: bool = True,
    att_ship: int = 587,
) -> dict:
    ts = datetime.now(UTC) - timedelta(seconds=ago)
    return {
        "killmail_id": kid,
        "killmail_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zkb": {"locationID": loc, "totalDroppableValue": droppable, "solo": solo, "npc": False},
        "victim": {"ship_type_id": ship},
        "attackers": [{"character_id": 1, "ship_type_id": att_ship, "weapon_type_id": 3033}],
    }


GATES: dict[str, set[int]] = {"1": {600}, "2": {601, 602}, "3": {603}, "4": {604}}
GATE_NAMES = {
    600: "Stargate (Beta)",
    601: "Stargate (Alpha)",
    602: "Stargate (Out)",
    603: "Stargate (Alpha)",
    604: "Stargate (Out)",
}
GATE_LABELS = {600: "Beta", 601: "Alpha", 602: "Out", 603: "Alpha", 604: "Out"}


def make_monitor(polls: dict[str, list[dict] | None], **kwargs) -> ZoneMonitor:
    async def fetcher(session, sid, window):
        return polls.get(sid)

    async def fake_ships(session, type_ids, cache):
        return {587: "Rifter", 670: "Capsule"}

    monitor = ZoneMonitor(request_gap=0, hed_ids=HED, fetcher=fetcher, **kwargs)
    zone_module.resolve_ship_names = fake_ships
    return monitor


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def run_ticks(monitor: ZoneMonitor, bot: FakeBot, times: int = 1) -> None:
    """Прогнать N тиков монитора и закрыть его сессию (без «Unclosed session»)."""
    for _ in range(times):
        asyncio.run(monitor._tick_all(bot))
    asyncio.run(monitor.aclose())


def start_zone(monitor: ZoneMonitor, chat_id: int = 7) -> None:
    monitor.start(chat_id, make_graph(), GATES, GATE_NAMES, GATE_LABELS)


def test_zone_preset_is_hed_plus_gate_neighbors() -> None:
    preset = build_zone_preset(make_graph(), HED)
    assert preset == ["1", "2", "3", "4"]  # 5 — без гейт-связи с Hed, не входит


def test_hour_trigger_fires_and_format_matches_spec() -> None:
    polls = {
        "3": [kill(i, 603, ago=120 * i) for i in range(1, 9)],  # 8 киллов за час
        "4": [],
    }
    monitor = make_monitor(polls)
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Сейчас гайки кемпят в системе:" in text
    assert "<blockquote expandable>Gamma 8 киллов и 8.00M ISK на гейтах" in text
    assert "на Alpha: 8 за час" in text
    assert "droppable 8.00M" in text
    assert "атака: соло · корабли: Rifter" in text
    assert "Статистика за последний час. Запрос репорта по /zone_status" in text
    assert "Местное время -" in text and "ET" in text
    assert "zKillboard" not in text  # ссылки убраны (§0.8)
    assert "Причина" not in text  # только конкретика (§0.8)


def test_no_burst_trigger_three_kills_stay_silent() -> None:
    """§0.7: 10-минутные триггеры удалены — 3 килла за 10 мин молча копятся за час."""
    polls = {
        "3": [kill(1, 603, ago=120), kill(2, 603, ago=240), kill(3, 603, ago=420)],
        "4": [],
    }
    monitor = make_monitor(polls)
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert bot.sent == []


def test_isk_trigger_single_expensive_kill() -> None:
    polls = {"3": [kill(1, 603, droppable=6e8, ago=300)], "4": []}  # 0.6B ≥ 0.5B
    monitor = make_monitor(polls)
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    assert "Gamma 1 килл и 600.00M ISK на гейтах" in bot.sent[0][1]


def test_delta_shows_only_for_recent_kills() -> None:
    # 8 киллов за час (триггер), из них только 2 — за последние 10 минут.
    polls = {
        "3": [kill(1, 603, ago=120), kill(2, 603, ago=240)]
        + [kill(i, 603, ago=900 + i * 120) for i in range(3, 9)],
        "4": [],
    }
    monitor = make_monitor(polls)
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert "на Alpha: 8 за час (+2 за последние 10 минут)" in bot.sent[0][1]


def test_anti_spam_no_repeat_without_new_kills() -> None:
    polls = {"3": [kill(i, 603, ago=120 * i) for i in range(1, 9)], "4": []}
    monitor = make_monitor(polls)
    monitor.cooldown = 0.0  # КД выключен — проверяем только анти-спам по новым киллам
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot, times=2)
    assert len(bot.sent) == 1  # второй тик без новых киллов — тишина


def test_cooldown_suppresses_repeat_alert() -> None:
    polls = {"3": [kill(i, 603, ago=120 * i) for i in range(1, 9)], "4": []}
    monitor = make_monitor(polls)
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot)
    # Новые киллы пришли, но cooldown (15 мин) ещё не прошёл — тишина.
    polls["3"] = polls["3"] + [kill(50, 603, ago=60)]
    asyncio.run(monitor._tick_all(FakeBot()))
    assert len(bot.sent) == 1
    asyncio.run(monitor.aclose())


def test_multi_system_alert_sorted_by_kills() -> None:
    polls = {
        "3": [kill(i, 603, ago=120 * i) for i in range(1, 9)],  # 8 киллов
        "4": [kill(100, 604, droppable=6e8, ago=300)],  # 1 килл, 0.6B
    }
    monitor = make_monitor(polls)
    start_zone(monitor)
    bot = FakeBot()
    run_ticks(monitor, bot)
    text = bot.sent[0][1]
    assert "Сейчас гайки кемпят в системах:" in text
    assert text.index("Gamma") < text.index("Delta")  # §0.12: горячие первыми


def test_attackers_toggle_removes_ships_line() -> None:
    polls = {"3": [kill(i, 603, ago=120 * i) for i in range(1, 9)], "4": []}
    monitor = make_monitor(polls)
    start_zone(monitor)
    monitor.watches[7].include_attack = False
    bot = FakeBot()
    run_ticks(monitor, bot)
    text = bot.sent[0][1]
    assert "атака: соло" in text
    assert "корабли:" not in text


def test_status_reports_from_events() -> None:
    polls = {"3": [kill(i, 603, ago=120 * i) for i in range(1, 9)], "4": []}
    monitor = make_monitor(polls)
    start_zone(monitor)
    run_ticks(monitor, FakeBot())
    status = asyncio.run(monitor.status(7))
    assert "Сейчас гайки кемпят в системе:" in status
    assert "✅ Чисто: 3/4 систем" in status
    clean_monitor = make_monitor({"3": [], "4": []})
    start_zone(clean_monitor)
    clean_status = asyncio.run(clean_monitor.status(7))
    assert "Сейчас гайки в зоне не кемпят." in clean_status
    assert "✅ Чисто: 4/4 систем" in clean_status
    asyncio.run(clean_monitor.aclose())


def test_live_snapshot_without_subscription() -> None:
    polls = {"3": [kill(i, 603, ago=120 * i) for i in range(1, 9)], "4": []}
    monitor = make_monitor(polls)
    graph = make_graph()
    snapshot = asyncio.run(monitor.live_snapshot(graph, GATES, GATE_NAMES, GATE_LABELS))
    assert "Сейчас гайки кемпят в системах:" in snapshot
    assert "Постоянное слежение: /zone_on" in snapshot
    assert "✅ Чисто: 3/4 систем" in snapshot
    empty = make_monitor({})
    clean = asyncio.run(empty.live_snapshot(graph, GATES, GATE_NAMES, GATE_LABELS))
    assert "Сейчас гайки в зоне не кемпят." in clean
    assert "✅ Чисто: 4 систем" in clean
    asyncio.run(empty.aclose())


def test_settings_bounds_v2() -> None:
    from gatecheck_bot.zone import SETTING_BOUNDS

    assert set(SETTING_BOUNDS) == {"hour", "isk", "cooldown"}
    assert SETTING_BOUNDS["hour"] == (4, 20)
