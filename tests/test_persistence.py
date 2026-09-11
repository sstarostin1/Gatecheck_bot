"""Рестарт-персистентность: подписки зоны и база маршрутов из SQLite."""

import asyncio
import time

from test_monitoring import GATE_NAMES as ROUTE_GATE_NAMES
from test_monitoring import GATES as ROUTE_GATES
from test_zone import GATE_NAMES, GATES, HED, FakeBot, run_ticks

from gatecheck_bot.monitoring import RouteMonitor
from gatecheck_bot.routing import Graph
from gatecheck_bot.storage import Storage
from gatecheck_bot.zone import ZoneMonitor


def make_graph() -> Graph:
    systems = {
        "1": {"name": "Alpha"},
        "2": {"name": "Beta"},
        "3": {"name": "Gamma"},
    }
    adjacency = {"1": ["2", "3"], "2": ["1", "3"], "3": ["1", "2"]}
    return Graph(systems=systems, adjacency=adjacency, meta={})


def make_zone(polls: dict[str, list[dict] | None], storage) -> ZoneMonitor:
    async def fetcher(session, sid, window):
        return polls.get(sid)

    async def fake_ships(session, type_ids, cache):
        return {}

    return ZoneMonitor(request_gap=0, hed_ids=HED, storage=storage, fetcher=fetcher)


def test_zone_subscription_survives_restart(tmp_path) -> None:
    storage = Storage(tmp_path / "p.sqlite3")
    graph = make_graph()
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}

    first = make_zone(polls, storage)
    first.start(7, graph, GATES, GATE_NAMES)
    assert storage.zone_sub_ids() == [7]

    # «Рестарт»: новый экземпляр монитора на том же Storage.
    second = make_zone(polls, storage)
    assert second.restore(graph, GATES, GATE_NAMES) == 1
    assert 7 in second.watches
    assert second.watches[7].systems  # пресет пересобран по графу
    run_ticks(second, FakeBot())
    storage.close()


def test_zone_cooldown_persisted_across_restart(tmp_path) -> None:
    storage = Storage(tmp_path / "p.sqlite3")
    graph = make_graph()
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}

    first = make_zone(polls, storage)
    first.start(7, graph, GATES, GATE_NAMES)
    storage.log_alert(7, "zone", "3", sent_epoch=time.time() - 60.0)  # алерт минуту назад

    second = make_zone(polls, storage)
    second.restore(graph, GATES, GATE_NAMES)
    watch = second.watches[7]
    # Cooldown ещё действует: после «рестарта» повторного алерта не будет.
    assert time.monotonic() - watch.last_alert["3"] < watch.cooldown
    storage.close()


def test_route_seen_base_persisted(tmp_path) -> None:

    storage = Storage(tmp_path / "p.sqlite3")
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}

    async def fetcher(session, sid, window):
        return polls.get(sid)

    async def fake_ships(type_ids):
        return {}

    graph = make_graph()
    first = RouteMonitor(request_gap=0, storage=storage, fetcher=fetcher)
    first._resolve_ship_names = fake_ships  # type: ignore[method-assign]
    first.start(9, ["1", "2", "3"], graph, ROUTE_GATES, ROUTE_GATE_NAMES)
    # Гейт 500 ∈ ROUTE_GATES["1"] — база киллов «на гейте».
    storage.add_kill_event(
        "1", 77, gate_id=500, kill_ts=time.time() - 600, droppable=1e6, ship_type_id=587
    )

    second = RouteMonitor(request_gap=0, storage=storage, fetcher=fetcher)
    second._resolve_ship_names = fake_ships  # type: ignore[method-assign]
    assert second.restore(graph, ROUTE_GATES, ROUTE_GATE_NAMES) == 1
    assert 77 in second.watches[9].seen
    asyncio.run(second.aclose())
    storage.close()