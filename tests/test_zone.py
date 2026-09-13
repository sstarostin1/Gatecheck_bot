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
GATE_NAMES = {
    600: "Stargate I",
    601: "Stargate II",
    602: "Stargate III",
    603: "Stargate IV",
    604: "Stargate V",
}


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
    assert "Всплеск активности в системе:" in text
    assert "Gamma — за 10 мин: 3 килл(ов) на гейтах, droppable ISK за 10 мин: 3.00M · за час: 3" in text
    assert "• Stargate IV: за 10 мин 3 · за час 3" in text
    assert "Корабли (за 10 мин): Rifter" in text
    assert "Причина" not in text  # триггеры убраны из текста — только конкретика
    assert "zKillboard" not in text  # ссылки убраны по фидбеку


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
    assert "Delta — за 10 мин: 0 килл(ов) на гейтах, droppable ISK за 10 мин: 0 · за час: 8" in text
    assert "• гейт 604: за 10 мин 0 · за час 8" in text or "• Stargate V: за 10 мин 0 · за час 8" in text
    assert "всплеск" not in text  # строчными — заголовок «Всплеск» не считается триггером


def test_isk_trigger_single_expensive_kill() -> None:
    polls = {"3": [kill(1, 603, droppable=700e6, ago=60)], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "droppable ISK за 10 мин: 700.00M" in text
    assert "Всплеск активности в системе:" in text


def test_no_repeat_without_new_kills() -> None:
    """Триггер активен, но новых киллов нет — повторных алертов не шлём (анти-спам)."""
    polls = {
        "3": [kill(1, 603, ago=120), kill(2, 603, ago=240), kill(3, 603, ago=420)],
        "4": [],
    }
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    run_ticks(monitor, bot)  # те же данные — ничего нового
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1


def test_multi_system_single_message() -> None:
    """Несколько систем сработали в один тик → одно сообщение со списком систем."""
    polls = {
        "3": [kill(1, 603, ago=120), kill(2, 603, ago=240), kill(3, 603, ago=420)],
        "4": [kill(10, 604, ago=120), kill(11, 604, ago=240), kill(12, 604, ago=420)],
    }
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    run_ticks(monitor, bot)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Всплеск активности в системах:" in text
    assert "Gamma — за 10 мин: 3" in text
    assert "Delta — за 10 мин: 3" in text


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
    assert "✅ Чисто: 4 систем" in asyncio.run(monitor.status(7))


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
    assert len(bot.sent) == 2  # снова алерт — есть новые киллы и триггер активен


def test_status_shows_per_gate_details() -> None:
    polls = {"3": [kill(1, 603, ago=120)], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    run_ticks(monitor, FakeBot())
    status = asyncio.run(monitor.status(7))
    assert "Gamma — за 10 мин: 1 килл(ов) на гейтах, droppable ISK за 10 мин: 1.00M · за час: 1" in status
    assert "• Stargate IV: за 10 мин 1 · за час 1" in status
    assert "Чисто: 3 систем" in status
    assert "не включена" not in status
    monitor.stop(7)
    assert "не включена" in asyncio.run(monitor.status(7))


def test_live_snapshot_without_subscription() -> None:
    """/zone status без подписки — разовый живой опрос зоны прямо сейчас."""
    polls = {"3": [kill(1, 603, ago=120), kill(2, 603, ago=300)], "4": []}
    monitor = make_monitor(polls)
    snapshot = asyncio.run(monitor.live_snapshot(make_graph(), GATES, GATE_NAMES))
    assert "Активность зоны «Hed + соседи» (разовый опрос, 4 систем):" in snapshot
    assert "Gamma — за 10 мин: 2 килл(ов) на гейтах" in snapshot
    assert "• Stargate IV: за 10 мин 2 · за час 2" in snapshot
    assert "Чисто: 3 систем" in snapshot
    assert "Постоянное слежение: /zone on" in snapshot


def test_events_pruned_after_hour_window() -> None:
    polls = {"3": [kill(i, 603, ago=3700 + i) for i in range(1, 6)], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    run_ticks(monitor, FakeBot())
    watch = monitor.watches[7]
    assert len(watch.events["3"]) == 0  # всё старше часового окна — вычищено


def test_force_tick_updates_immediately() -> None:
    """Кнопка «Обновить сейчас»: тик по требованию — события и статус свежие."""
    polls: dict[str, list[dict] | None] = {"3": [], "4": []}
    monitor = make_monitor(polls)
    monitor.start(7, make_graph(), GATES, GATE_NAMES)
    run_ticks(monitor, FakeBot())
    polls["3"] = [kill(1, 603, ago=120), kill(2, 603, ago=240), kill(3, 603, ago=420)]
    bot = FakeBot()
    asyncio.run(monitor.force_tick(7, bot))
    assert len(bot.sent) == 1  # алерт по новым киллам пришёл сразу, вне расписания
    status = asyncio.run(monitor.status(7))
    assert "Gamma — за 10 мин: 3 килл(ов) на гейтах" in status