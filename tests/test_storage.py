"""Тесты SQLite-хранилища: миграции, подписки, маршруты, kill-кэш, алерты."""

from gatecheck_bot.storage import Storage


def make_storage(tmp_path) -> Storage:
    return Storage(tmp_path / "test.sqlite3")


def test_migrations_create_tables(tmp_path) -> None:
    s = make_storage(tmp_path)
    tables = {
        row["name"]
        for row in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "users",
        "zone_subs",
        "route_watches",
        "kill_events",
        "alerts_log",
        "_migrations",
    } <= tables
    s.close()


def test_migrations_applied_once(tmp_path) -> None:
    s = make_storage(tmp_path)
    count = s._conn.execute("SELECT COUNT(*) AS n FROM _migrations").fetchone()["n"]
    assert count == 1
    s.close()


def test_zone_subscriptions(tmp_path) -> None:
    s = make_storage(tmp_path)
    assert s.zone_sub_ids() == []
    s.add_zone_sub(7)
    s.add_zone_sub(7)  # идемпотентно
    s.add_zone_sub(8)
    assert sorted(s.zone_sub_ids()) == [7, 8]
    s.del_zone_sub(7)
    assert s.zone_sub_ids() == [8]
    s.close()


def test_user_settings_roundtrip(tmp_path) -> None:
    s = make_storage(tmp_path)
    assert s.get_settings(1) == {}
    s.set_settings(1, {"burst": 4})
    s.set_settings(1, {"burst": 5, "isk_burst": 2e8})
    assert s.get_settings(1) == {"burst": 5, "isk_burst": 2e8}
    s.close()


def test_routes_active_and_expiry(tmp_path) -> None:
    s = make_storage(tmp_path)
    s.upsert_route(5, ["1", "2", "3"], started_epoch=100.0, expires_epoch=3600.0)
    s.upsert_route(6, ["9"], started_epoch=100.0, expires_epoch=50.0)  # истёк
    active = s.active_routes(now_epoch=200.0)
    assert len(active) == 1
    assert active[0]["chat_id"] == 5
    assert active[0]["route"] == ["1", "2", "3"]
    s.upsert_route(5, ["1"], 100.0, 99999.0)  # замена маршрута того же чата
    assert s.active_routes(200.0)[0]["route"] == ["1"]
    s.del_route(5)
    assert s.active_routes(200.0) == []
    s.close()


def test_kill_events_add_query_prune(tmp_path) -> None:
    s = make_storage(tmp_path)
    s.add_kill_event("1", 10, 600, kill_ts=1000.0, droppable=1e6, ship_type_id=587)
    s.add_kill_event("1", 10, 600, kill_ts=1000.0, droppable=1e6, ship_type_id=587)  # дубликат
    s.add_kill_event("2", 11, 601, kill_ts=9000.0, droppable=2e6, ship_type_id=None)
    assert [e["kill_id"] for e in s.kill_events_since(950.0)] == [10, 11]  # оба >= 950
    assert len(s.kill_events_since(0.0)) == 2  # дубликат не вставлен
    s.prune_kill_events(before_epoch=9500.0)  # 1000 и 9000 < 9500 — оба удалятся
    assert s.kill_events_since(0.0) == []
    s.close()


def test_alerts_last_epoch(tmp_path) -> None:
    s = make_storage(tmp_path)
    s.log_alert(1, "zone", "3", sent_epoch=100.0)
    s.log_alert(1, "zone", "3", sent_epoch=500.0)
    s.log_alert(2, "route", "9", sent_epoch=300.0)
    assert s.last_alert_epochs("zone") == {(1, "3"): 500.0}
    assert s.last_alert_epochs("route") == {(2, "9"): 300.0}
    s.close()