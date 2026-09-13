"""Тесты Telegram-клавиатур: состав reply-меню и inline-кнопки обновления."""

from gatecheck_bot.ui import REFRESH_TEXT, main_keyboard, refresh_inline


def test_main_keyboard_rows() -> None:
    kb = main_keyboard()
    rows = [[btn.text for btn in row] for row in kb.keyboard]
    assert rows[0] == [REFRESH_TEXT]
    assert rows[1] == ["/zone status", "/zone on"]
    assert rows[2] == ["/route status", "/route stop"]
    assert rows[3] == ["/settings", "/ping"]
    assert kb.resize_keyboard is True


def test_refresh_inline_points_to_status_refresh() -> None:
    kb = refresh_inline()
    assert kb.inline_keyboard[0][0].callback_data == "refresh:status"
    assert "Обновить" in kb.inline_keyboard[0][0].text