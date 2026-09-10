"""Тесты монитора зоны: пресет, окна D5 (всплеск/час/ISK), cooldown, статусы."""

import asyncio
from datetime import UTC, datetime, timedelta

import gatecheck_bot.zone as zone_module
from gatecheck_bot.monitoring import filter_gate_kills
from gatecheck_bot.routing import Graph
from gatecheck_bot.zone import ZoneMonitor, build_zone_preset


def make_graph() -> Graph:
    systems = {
        "1": {"name": "Alpha"},
        "2": {"name": "Beta"},
        "3": {"name": "Gamma"},
        "4": {"name": "Delta"},
        "5": {"name": "Faraway"},  # не соединён с Hed-системами
    }
    adjacency = {"1": ["2", "3"], "2": ["1", "4"], "3": ["1"], "4": ["2"]}
    return Graph(systems=systems, adjacency=adjacency, meta={})


HED = frozenset({"1", "2"})


def kill(kid: int, loc: int, droppable: float = 1e6, ship: int = 587, ago: float = 60.0) -> dict:
    ts = datetime.now(UTC) - timedelta(seconds=ago)
    return {
        "killmail_id": kid,
        "killmail_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zkb": {"locationID": loc, "totalDroppableValue": droppable},
        "victim": {"ship_type_id": ship},
    }


GATES: dict[str, set[int]] = {"1": {600}, "2": {601, 602}, "3": {603}, "4": {604}}
GATE_NAMES = {600: "Stargate I", 601: "Stargate II", 602: "Stargate III"}


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


def test_zone_preset_is_hed_plus_gate_neighbors() -> None:
    preset = build_zone_preset(make_graph(), HED)
    assert preset == ["1", "2", "3", "4"]  # 5 — без гейт-связи с Hed, не входит


def test_filter_gate_kills_shared_with_route_monitor() -> None:
    assert filter_gate_kills([kill(1, 603)], {603, 604}) != []


def test_burst_trigger_fires() -> None:
    polls = {
        "3": [kill(1, 603, ago=120), kill(2, 603, ago=240), kill(3, 603, ago=420)],
        "4": [],
    }
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "🔥 Зона фарма: Gamma" in text
    assert "всплеск ≥3/10 мин" in text
    assert "zkillboard.com/system/3" in text


def test_hour_accumulation_without_burst() -> None:
    polls = {
        "4": [kill(i, 604, ago=900 + i * 120) for i in range(1, 9)],  # 8 киллов, все старше 15 мин
        "3": [],
    }
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "накопление ≥8/час" in text
    assert "всплеск" not in text


def test_isk_trigger_single_expensive_kill() -> None:
    polls = {"3": [kill(1, 603, droppable=150e6, ago=60)], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    assert "droppable ISK" in bot.sent[0][1]


def test_station_kills_do_not_trigger() -> None:
    polls = {
        "3": [kill(i, 60004816, ago=120) for i in range(1, 6)],  # станция — не гейт
        "4": [],
    }
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert bot.sent == []
    assert "✅ Чисто: 4 систем" in monitor.status(7)


def test_cooldown_suppresses_repeats() -> None:
    polls: dict[str, list[dict] | None] = {
        "3": [kill(1, 603, ago=60), kill(2, 603, ago=120), kill(3, 603, ago=180)],
        "4": [],
    }
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    polls["3"] = polls["3"] + [kill(4, 603, ago=30)]
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1  # cooldown: повтор подавлен
    monitor.watches[7].last_alert["3"] = 0.0  # cooldown истёк
    run_ticks(monitor, bot)
    assert len(bot.sent) == 2  # снова алерт — триггер ещё активен


def test_status_shows_counts_and_clean() -> None:
    polls = {"3": [kill(1, 603, ago=120)], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    run_ticks(monitor, FakeBot())
    status = monitor.status(7)
    assert "Gamma: за 10 мин 1, за час 1" in status
    assert "Чисто: 3 систем" in status
    assert "не включена" not in status
    monitor.stop(7)
    assert "не включена" in monitor.status(7)


def test_events_pruned_after_hour_window() -> None:
    polls = {"3": [kill(i, 603, ago=3700 + i) for i in range(1, 6)], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    run_ticks(monitor, FakeBot())
    watch = monitor.watches[7]
    assert len(watch.events["3"]) == 0  # всё старше часового окна — вычищено