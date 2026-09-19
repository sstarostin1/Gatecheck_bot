"""Клавиатуры Telegram v0.10 (спека docs/MESSAGES.md §13).

Reply-меню — человекочитаемые кнопки под полем ввода; обработка — по точному тексту
(F.text == label) в handlers.py. Inline «🔄 Обновить» — ТОЛЬКО на зональных репортах
(§1–§4, callback refresh:zone); на сообщениях маршрута inline-кнопок нет.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

REFRESH_TEXT = "🔄 Гейты: обновить"   # тик зон + отчёт §3/§4 (обновляет ТОЛЬКО зоны)
BTN_ZONE_ON = "🛡 Зона: вкл"
BTN_ZONE_OFF = "🛡 Зона: выкл"
BTN_ROUTE_STATUS = "🚀 Маршрут: статус"
BTN_ROUTE_STOP = "🚀 Маршрут: стоп"
BTN_SETTINGS = "⚙️ Пороги"


def main_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню под полем ввода: частые действия в один тап (§13)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=REFRESH_TEXT)],
            [KeyboardButton(text=BTN_ZONE_ON), KeyboardButton(text=BTN_ZONE_OFF)],
            [KeyboardButton(text=BTN_ROUTE_STATUS), KeyboardButton(text=BTN_ROUTE_STOP)],
            [KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Команда или кнопка меню…",
    )


def refresh_inline() -> InlineKeyboardMarkup:
    """Inline-кнопка «Обновить» — только под зональными репортами (§13)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh:zone")]]
    )
