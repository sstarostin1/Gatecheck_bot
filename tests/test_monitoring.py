"""Тесты маршрутного монитора v0.10: вердикт §6, пуш только по 🚩-гейтам §7, TTL."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import gatecheck_bot.monitoring as monitoring_module
from gatecheck_bot.monitoring import RouteMonitor, extract_features, route_verdict
from gatecheck_bot.routing import Graph


def make_graph() -> Graph:
    systems = {
        "1": {"name": "Alpha", "security_status": 0.5},
        "2": {"name": "Beta", "security_status": 0.4},
        "3": {"name": "Gamma", "security_status": 0.3},
    }
    adjacency = {"1": ["2"], "2": ["1", "3"], "3": ["2"]}
    return Graph(systems=systems, adjacency=adjacency, meta={})


def kill(
    kid: int,
    loc: int,
    value: float = 1e6,
    ship: int = 587,
    ago: float = 60.0,
    att_ship: int = 587,
    weapon: int = 3033,
) -> dict:
    ts = datetime.now(UTC) - timedelta(seconds=ago)
    return {
        "killmail_id": kid,
        "killmail_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zkb": {"locationID": loc, "totalDroppableValue": value, "solo": True, "npc": False},
        "victim": {"ship_type_id": ship},
        "attackers": [{"character_id": 1, "ship_type_id": att_ship, "weapon_type_id": weapon}],
    }


# Гейты: 500 ведёт 1→2 (маршрутный для системы «1»), 502 — из «1» наружу (не маршрутный),
# 501 — 2→1 (маршрутный), 503 — 3→2 (маршрутный).
GATES: dict[str, set[int]] = {"1": {500, 502}, "2": {501}, "3": {503}}
GATE_NAMES = {500: "Stargate (Beta)", 501: "Stargate (Alpha)", 502: "Stargate (Far)", 503: "Stargate (Beta)"}
GATE_DEST = {500: "2", 501: "1", 502: "9", 503: "2"}


def make_monitor(polls: dict[str, list[dict] | None], **kwargs) -> RouteMonitor:
    async def fetcher(session, sid, window):
        return polls.get(sid)

    async def fake_ships(session, type_ids, cache):
        return {587: "Rifter", 22456: "Sabre"}

    monitor = RouteMonitor(request_gap=0, fetcher=fetcher, **kwargs)
    monitoring_module.resolve_ship_names = fake_ships
    monitoring_module.smartbomb_type_ids = _fake_bombs  # type: ignore[assignment]
    return monitor


async def _fake_bombs(session):
    return frozenset({17926})  # SDE-группа 55 (тестовый стенд)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def begin(monitor: RouteMonitor, chat_id: int = 100, route: list[str] | None = None):
    return monitor.begin(chat_id, route or ["1", "2", "3"], make_graph(), GATES, GATE_NAMES, GATE_DEST)


def test_route_gates_computed_from_destinations() -> None:
    monitor = make_monitor({})
    watch = begin(monitor)
    assert watch.route_gates["1"] == {500}  # 502 ведёт наружу — не маршрутный
    assert watch.route_gates["2"] == {501}
    assert watch.route_gates["3"] == {503}
    asyncio.run(monitor.aclose())


def test_extract_features_bombs_and_bubbles() -> None:
    bomb_kill = kill(1, 500, weapon=17926, att_ship=22456)  # смартбомба + диктор
    feature = extract_features(bomb_kill, frozenset({17926}))
    assert feature["bomb"] is True
    assert feature["bubble"] is True  # и пусковик-признак, и диктор в атаке
    plain = extract_features(kill(2, 500, weapon=3033, att_ship=587), frozenset({17926}))
    assert plain["bomb"] is False
    assert plain["bubble"] is False


def test_snapshot_no_alerts_and_verdict_clean() -> None:
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}
    monitor = make_monitor(polls)
    watch = begin(monitor)
    ok = asyncio.run(monitor.snapshot(watch))
    assert ok is True
    assert watch.baselined is True
    level, levels = route_verdict(watch, time.time())
    assert level == 0 and levels[1] == [] and levels[2] == []
    asyncio.run(monitor.aclose())


def test_alert_only_on_route_gate() -> None:
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}
    monitor = make_monitor(polls)
    watch = begin(monitor)
    asyncio.run(monitor.snapshot(watch))
    bot = FakeBot()
    # Новый килл на НЕмаршрутном гейте (502) — тишина (§7: меняет вердикт на 🟡).
    polls["1"] = [kill(10, 502, ago=30)]
    asyncio.run(monitor.force_tick(100, bot, send=True))
    assert bot.sent == []
    # Новый килл на маршрутном гейте (500) — пуш сразу, без cooldown (§0.10).
    polls["1"] = polls["1"] + [kill(11, 500, ago=30)]
    asyncio.run(monitor.force_tick(100, bot, send=True))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "🚨 Кемп на маршруте (точка 1/3):" in text
    assert "<blockquote expandable>" in text
    assert "🚩 на Beta: 1 за час (+1 за последние 10 минут)" in text
    assert "атака: соло · корабли: Rifter" in text
    assert "droppable" not in text  # сумма выпавшего на маршруте не показывается (§7)
    assert "🛡 Слежение маршрута Alpha → Gamma — осталось" in text
    assert "Остановить слежение: /route_stop" in text
    asyncio.run(monitor.aclose())


def test_verdict_levels_and_status() -> None:
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}
    monitor = make_monitor(polls)
    watch = begin(monitor)
    asyncio.run(monitor.snapshot(watch))
    # Килл на прочем гейте системы маршрута → 🟡 (без пуша).
    watch.events["1"].append(
        {**extract_features(kill(20, 502, ago=60), frozenset()), "ts": time.time() - 60}
    )
    status = asyncio.run(monitor.status(100))
    assert "🟡" in status and "Безопасность под вопросом" in status
    # Килл на маршрутном гейте → 🟠; с признаками бабблов → 🔴.
    watch.events["2"].append(
        {
            **extract_features(kill(21, 501, ago=60, att_ship=22456), frozenset()),
            "ts": time.time() - 60,
        }
    )
    status = asyncio.run(monitor.status(100))
    assert "🔴" in status and "Маршрут очень опасен" in status
    assert "Сейчас гайки кемпят на маршруте:" in status
    asyncio.run(monitor.aclose())


def test_ttl_expiry_message_and_cleanup() -> None:
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}
    monitor = make_monitor(polls)
    begin(monitor)
    bot = FakeBot()
    asyncio.run(monitor.snapshot(monitor.watches[100]))
    monitor.watches[100].expires_at = 0.0  # TTL истёк
    asyncio.run(monitor._tick_all(bot))
    assert 100 not in monitor.watches
    assert "TTL" in bot.sent[-1][1]
    asyncio.run(monitor.aclose())


def test_stop_and_status_inactive() -> None:
    monitor = make_monitor({})
    assert "не активно" in monitor.stop(1)
    assert "не активно" in asyncio.run(monitor.status(1))
    begin(monitor, chat_id=1)
    assert "остановлено" in monitor.stop(1)
    asyncio.run(monitor.aclose())


def test_route_watch_persisted_and_restored(tmp_path) -> None:
    from gatecheck_bot.storage import Storage

    storage = Storage(tmp_path / "p.sqlite3")
    polls: dict[str, list[dict] | None] = {"1": [], "2": [], "3": []}
    monitor = make_monitor(polls, storage=storage)
    monitor.begin(9, ["1", "2", "3"], make_graph(), GATES, GATE_NAMES, GATE_DEST)
    asyncio.run(monitor.snapshot(monitor.watches[9]))
    storage.add_kill_event(
        "2",
        77,
        gate_id=501,
        kill_ts=time.time() - 600,
        droppable=1e6,
        ship_type_id=587,
        features={"ts": time.time() - 600, "kid": 77, "gate": 501, "droppable": 1e6,
                  "ship": 587, "solo": True, "npc": False, "bomb": False, "bubble": False,
                  "att_ships": [587]},
    )
    asyncio.run(monitor.aclose())
    storage.close()

    second = make_monitor({}, storage=Storage(tmp_path / "p.sqlite3"))
    # Тот же файл: пересоздаём Storage на том же пути через новый Storage().
    assert second.restore(make_graph(), GATES, GATE_NAMES, GATE_DEST) == 1
    watch = second.watches[9]
    assert watch.route == ["1", "2", "3"]
    assert watch.baselined is True  # база была до рестарта
    assert 77 in watch.seen  # база из kill-кэша: после рестарта дублей не будет
    assert any(e["kid"] == 77 for e in watch.events["2"])  # события восстановлены с признаками
    asyncio.run(second.aclose())
    second.storage.close()
