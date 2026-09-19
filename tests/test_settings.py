"""Границы и парсинг пользовательских порогов D5 v2 (§0.7, §11)."""

from gatecheck_bot.zone import SETTING_BOUNDS, clamp_setting, parse_setting_value


def test_bounds_are_sane() -> None:
    assert set(SETTING_BOUNDS) == {"hour", "isk", "cooldown"}  # burst удалён (§0.7)
    for low, high in SETTING_BOUNDS.values():
        assert 0 < low <= high


def test_bounds_match_spec() -> None:
    assert SETTING_BOUNDS["hour"] == (4, 20)
    assert SETTING_BOUNDS["isk"] == (1e7, 5e9)
    assert SETTING_BOUNDS["cooldown"] == (300, 3600)


def test_clamp_limits_values() -> None:
    assert clamp_setting("hour", 2) == 4
    assert clamp_setting("hour", 99) == 20
    assert clamp_setting("isk", 5e6) == 1e7  # ниже границы
    assert clamp_setting("cooldown", 7200.0) == 3600.0  # выше границы


def test_parse_values_with_units() -> None:
    assert parse_setting_value("hour", "12") == 12.0
    assert parse_setting_value("isk", "0.7B") == 7e8
    assert parse_setting_value("isk", "1,5B") == 1.5e9
    assert parse_setting_value("isk", "500M") == 5e8
    assert parse_setting_value("cooldown", "20м") == 1200.0
    assert parse_setting_value("cooldown", "30m") == 1800.0
    assert parse_setting_value("cooldown", "1ч") == 3600.0
    assert parse_setting_value("cooldown", "20") == 1200.0  # без суффикса — минутами


def test_parse_invalid_returns_none() -> None:
    assert parse_setting_value("hour", "abc") is None
    assert parse_setting_value("hour", "") is None
    assert parse_setting_value("hour", "-3") is None
