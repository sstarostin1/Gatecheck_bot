"""Тесты монитора маршрутов: фильтр гейтов, дедуп, алерты, TTL, статусы."""

import asyncio

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


def kill(kid: int, location: int, value: float = 1e6, ship: int = 670) -> dict:
    return {
        "killmail_id": kid,
        "zkb": {"locationID": location, "totalValue": value},
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
    assert "ISK на гейтах за час: 501.00M" in text  # 5e8 (новый) + 1e6 (старый)
    assert "Rifter" in text
    assert 3 not in monitor.watches[100].seen  # станция в дедуп не попала


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