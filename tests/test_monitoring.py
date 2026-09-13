"""Тесты монитора маршрутов: фильтр гейтов, дедуп, алерты, TTL, статусы."""

import asyncio
import time

from gatecheck_bot.monitoring import (
    RouteMonitor,
    filter_gate_kills,
    format_isk,
)
from gatecheck_bot.routing import Graph


def make_graph() -> Graph:
    systems = {
        "1": {"name": "Alpha"},
        "2": {"name": "Beta"},
        "3": {"name": "Gamma"},
    }
    adjacency = {"1": ["2"], "2": ["1", "3"], "3": ["2"]}
    return Graph(systems=systems, adjacency=adjacency, meta={})


def kill(kid: int, location: int, value: float = 1e6, ship: int = 587) -> dict:
    return {
        "killmail_id": kid,
        "killmail_time": "2026-09-09T12:00:00Z",
        "zkb": {"locationID": location, "totalValue": value, "totalDroppableValue": value},
        "victim": {"ship_type_id": ship},
    }


GATES: dict[str, set[int]] = {"1": {500}, "2": {501, 502}, "3": {503}}
GATE_NAMES = {
    500: "Stargate (Out)",
    501: "Stargate (Alpha)",
    502: "Stargate (Gamma)",
    503: "Stargate (Beta)",
}


def make_monitor(polls: dict[str, list[dict] | None]) -> RouteMonitor:
    async def fetcher(session, sid, window):
        return polls.get(sid)

    async def fake_ships(type_ids):
        return {670: "Capsule", 587: "Rifter"}

    monitor = RouteMonitor(
        poll_interval=1, ttl_seconds=3600, request_gap=0, fetcher=fetcher
    )
    monitor._resolve_ship_names = fake_ships  # type: ignore[method-assign]
    return monitor


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def test_filter_gate_kills_excludes_stations_and_planets() -> None:
    kills = [kill(1, 501), kill(2, 60004816), kill(3, 40161470)]
    out = filter_gate_kills(kills, {501, 502})
    assert [k["killmail_id"] for k in out] == [1]


def test_format_isk() -> None:
    assert format_isk(1.23e9) == "1.23B"
    assert format_isk(4.56e6) == "4.56M"
    assert format_isk(789.0) == "789"


def test_first_tick_is_baseline_no_alerts() -> None:
    polls = {"1": [kill(1, 500)], "2": [kill(2, 501), kill(3, 60004816)], "3": []}
    monitor = make_monitor(polls)
    monitor.start(100, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    asyncio.run(monitor._tick_all(bot))
    assert bot.sent == []  # первый тик — база, без алертов
    watch = monitor.watches[100]
    assert watch.counts == {"1": 1, "2": 1, "3": 0}  # станция отфильтрована
    assert watch.seen == {1, 2}


def test_second_tick_alerts_only_new_gate_kills() -> None:
    polls: dict[str, list[dict] | None] = {
        "1": [kill(1, 500)],
        "2": [kill(2, 501), kill(3, 60004816)],
        "3": [],
    }
    monitor = make_monitor(polls)
    monitor.start(100, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    asyncio.run(monitor._tick_all(bot))
    polls["2"] = [kill(2, 501), kill(3, 60004816), kill(10, 502, value=5e8, ship=587)]
    asyncio.run(monitor._tick_all(bot))
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 100
    assert "Гейт-камп: Beta" in text
    assert "Stargate (Gamma): +1 новых" in text
    assert "Droppable ISK на гейтах за час: 501.00M" in text  # 5e8 (новый) + 1e6 (старый)
    assert "Rifter" in text
    assert 3 not in monitor.watches[100].seen  # станция в дедуп не попала


def test_route_alert_groups_ship_and_pod_and_links_zkb() -> None:
    polls: dict[str, list[dict] | None] = {"1": [kill(1, 500)], "2": [], "3": []}
    monitor = make_monitor(polls)
    monitor.start(100, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    asyncio.run(monitor._tick_all(bot))
    polls["1"] = [
        kill(1, 500),
        kill(10, 500, value=7e8, ship=670),  # капсула
        kill(11, 500, value=8e8, ship=587),
    ]
    asyncio.run(monitor._tick_all(bot))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "Stargate (Out): +1 новых, +1 капсул(ы), за час 3" in text  # OQ-4; 1+1+1=3 за час
    assert "Droppable ISK на гейтах за час: 1.50B" in text  # 7e8 + 8e8
    assert "zKillboard" not in text  # ссылки убраны по фидбеку (превью zK спамило)


def test_fetch_hour_counts_alive_and_failed() -> None:
    polls: dict[str, list[dict] | None] = {
        "1": [kill(1, 500), kill(2, 500), kill(3, 60004816)],  # 2 из 3 на гейтах
        "2": None,  # сбой fetcher'а
        "3": [],
    }
    monitor = make_monitor(polls)
    counts = asyncio.run(monitor.fetch_hour_counts(["1", "2", "3"], GATES))
    assert counts["1"] == 2
    assert counts["2"] is None
    assert counts["3"] == 0


def test_route_watch_persisted_and_restored(tmp_path) -> None:
    from gatecheck_bot.storage import Storage

    storage = Storage(tmp_path / "p.sqlite3")
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}

    async def fetcher(session, sid, window):
        return polls.get(sid)

    async def fake_ships(type_ids):
        return {}

    first = RouteMonitor(request_gap=0, storage=storage, fetcher=fetcher)
    first._resolve_ship_names = fake_ships  # type: ignore[method-assign]
    first.start(9, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES)
    storage.add_kill_event(
        "2", 77, gate_id=501, kill_ts=time.time() - 600, droppable=1e6, ship_type_id=587
    )

    second = RouteMonitor(request_gap=0, storage=storage, fetcher=fetcher)
    second._resolve_ship_names = fake_ships  # type: ignore[method-assign]
    assert second.restore(make_graph(), GATES, GATE_NAMES) == 1
    watch = second.watches[9]
    assert watch.route == ["1", "2", "3"]
    assert watch.baselined is True  # база была до рестарта
    assert 77 in watch.seen  # база из kill-кэша: после рестарта дублей не будет
    asyncio.run(second.aclose())
    storage.close()


def test_status_progression_and_ttl_expiry() -> None:
    polls: dict[str, list[dict] | None] = {"1": [kill(1, 500)], "2": [], "3": []}
    monitor = make_monitor(polls)
    monitor.start(100, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES)
    bot = FakeBot()
    asyncio.run(monitor._tick_all(bot))
    assert "чисто" in monitor.status(100)
    polls["1"] = [kill(1, 500), kill(9, 500)]
    asyncio.run(monitor._tick_all(bot))
    assert "▲1" in monitor.status(100)
    monitor.watches[100].expires_at = 0.0  # TTL истёк
    asyncio.run(monitor._tick_all(bot))
    assert 100 not in monitor.watches
    assert "TTL" in bot.sent[-1][1]


def test_stop_and_status_inactive() -> None:
    monitor = make_monitor({})
    assert "не активно" in monitor.status(1)
    assert "не было включено" in monitor.stop(1)
    monitor.start(1, ["1"], make_graph(), GATES, GATE_NAMES)
    assert "остановлено" in monitor.stop(1)


def test_force_tick_updates_immediately() -> None:
    """Кнопка «Обновить сейчас»: тик по требованию — алерт и статус свежие."""
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}
    monitor = make_monitor(polls)
    monitor.start(100, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES)
    asyncio.run(monitor._tick_all(FakeBot()))
    polls["2"] = [kill(10, 502, value=5e8, ship=587)]
    bot = FakeBot()
    asyncio.run(monitor.force_tick(100, bot))
    assert len(bot.sent) == 1  # алерт пришёл по требованию, вне расписания
    assert "▲1" in monitor.status(100)
    asyncio.run(monitor.aclose())