"""Границы и парсинг пользовательских порогов D5 (OQ-8, /settings)."""

from gatecheck_bot.zone import SETTING_BOUNDS, clamp_setting, parse_setting_value


def test_bounds_are_sane() -> None:
    assert set(SETTING_BOUNDS) == {"burst", "hour", "isk_burst", "cooldown"}
    for low, high in SETTING_BOUNDS.values():
        assert 0 < low <= high


def test_clamp_limits_values() -> None:
    assert clamp_setting("burst", 1) == 2
    assert clamp_setting("burst", 99) == 10
    assert clamp_setting("burst", 4) == 4
    assert clamp_setting("isk_burst", 5e6) == 10e6  # ниже границы
    assert clamp_setting("cooldown", 7200.0) == 3600.0  # выше границы


def test_parse_values_with_units() -> None:
    assert parse_setting_value("burst", "4") == 4.0
    assert parse_setting_value("hour", "12") == 12.0
    assert parse_setting_value("isk_burst", "200M") == 2e8
    assert parse_setting_value("isk_burst", "1,5B") == 1.5e9
    assert parse_setting_value("isk_burst", "500K") == 5e5
    assert parse_setting_value("cooldown", "20m") == 1200.0
    assert parse_setting_value("cooldown", "30мин") == 1800.0
    assert parse_setting_value("cooldown", "1ч") == 3600.0
    assert parse_setting_value("cooldown", "900") == 54000.0  # без суффикса — минутами


def test_parse_invalid_returns_none() -> None:
    assert parse_setting_value("burst", "abc") is None
    assert parse_setting_value("burst", "") is None
    assert parse_setting_value("burst", "-3") is None