"""Тесты Telegram-клавиатур v0.10 (§13): emoji-лейблы, inline только для зон."""

from gatecheck_bot.ui import (
    BTN_ROUTE_STATUS,
    BTN_ROUTE_STOP,
    BTN_SETTINGS,
    BTN_ZONE_OFF,
    BTN_ZONE_ON,
    REFRESH_TEXT,
    main_keyboard,
    refresh_inline,
)


def test_main_keyboard_rows() -> None:
    kb = main_keyboard()
    rows = [[btn.text for btn in row] for row in kb.keyboard]
    assert rows[0] == [REFRESH_TEXT]
    assert rows[0] == ["🔄 Гейты: обновить"]
    assert rows[1] == [BTN_ZONE_ON, BTN_ZONE_OFF]
    assert rows[2] == [BTN_ROUTE_STATUS, BTN_ROUTE_STOP]
    assert rows[3] == [BTN_SETTINGS]
    assert kb.resize_keyboard is True


def test_refresh_inline_points_to_zone_refresh_only() -> None:
    kb = refresh_inline()
    assert kb.inline_keyboard[0][0].callback_data == "refresh:zone"  # §13: только зоны
    assert "Обновить" in kb.inline_keyboard[0][0].text
