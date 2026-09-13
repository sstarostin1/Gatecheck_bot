"""Клавиатуры Telegram: основное reply-меню (под полем ввода) и inline-кнопки обновления.

Кнопки reply-клавиатуры отправляют настоящие команды — так весь флоу остаётся
на существующих хэндлерах, а пользователю не нужно ничего печатать.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

REFRESH_TEXT = "🔄 Обновить сейчас"


def main_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню под полем ввода: частые действия в один тап."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=REFRESH_TEXT)],
            [KeyboardButton(text="/zone status"), KeyboardButton(text="/zone on")],
            [KeyboardButton(text="/route status"), KeyboardButton(text="/route stop")],
            [KeyboardButton(text="/settings"), KeyboardButton(text="/ping")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Команда или «🔄 Обновить сейчас»…",
    )


def refresh_inline() -> InlineKeyboardMarkup:
    """Inline-кнопка «Обновить» под сообщениями статуса зоны/маршрута."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh:status")]
        ]
    )
