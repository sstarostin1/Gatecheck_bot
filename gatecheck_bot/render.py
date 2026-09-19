"""HTML-рендеринг сообщений (спека docs/MESSAGES.md v1.1).

Разметка (§0.11): заголовки — <b> (в прототипах **…**); футеры/подсказки — <i>
(в прототипах __…__); копируемые списки (системы маршрута) — <pre>; блоки систем —
<blockquote expandable>. Все данные экранируются esc() — бот работает в HTML parse mode.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

__all__ = [
    "bold",
    "code",
    "esc",
    "format_isk",
    "italic",
    "kills_word",
    "pre",
    "quote",
    "route_footer",
    "systems_word",
    "zone_footer",
]


def esc(value: object) -> str:
    """Экранировать данные для HTML parse mode (все вставки — только через esc)."""
    return html.escape(str(value), quote=False)


def format_isk(isk: float) -> str:
    """ISK в человекочитаемый вид: 1.23B / 456.7M / 12.3K."""
    if isk >= 1e12:
        return f"{isk / 1e12:.2f}T"
    if isk >= 1e9:
        return f"{isk / 1e9:.2f}B"
    if isk >= 1e6:
        return f"{isk / 1e6:.2f}M"
    if isk >= 1e3:
        return f"{isk / 1e3:.1f}K"
    return f"{isk:.0f}"


def kills_word(n: int) -> str:
    """Плюрализация киллов (§1): 1 килл · 2–4 килла · 5+ киллов."""
    if n % 10 == 1 and n % 100 != 11:
        return "килл"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "килла"
    return "киллов"


def systems_word(n: int) -> str:
    """Плюрализация систем: 1 система · 2–4 системы · 5+ систем."""
    if n % 10 == 1 and n % 100 != 11:
        return "система"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "системы"
    return "систем"


def bold(text: str) -> str:
    return f"<b>{text}</b>"


def italic(text: str) -> str:
    return f"<i>{text}</i>"


def code(text: str) -> str:
    return f"<code>{text}</code>"


def pre(text: str) -> str:
    """Моноширинный блок без ссылок/разметки: список систем для EVE Notepad (§6)."""
    return f"<pre>{text}</pre>"


def quote(body: str) -> str:
    """Сворачиваемый блок системы (§0.6): до раскрытия видна только первая строка."""
    return f"<blockquote expandable>{body}</blockquote>"


def eve_time() -> str:
    """EVE Time (ET) = UTC+0, формат HH:MM (§0.3)."""
    return datetime.now(UTC).strftime("%H:%M")


def zone_footer() -> str:
    """Footer всех зональных репортов (§0.4, курсив)."""
    return italic(
        "Статистика за последний час. Запрос репорта по /zone_status. "
        f"Местное время - {eve_time()} ET"
    )


def route_footer() -> str:
    """Footer всех маршрутных сообщений (курсив)."""
    return italic(
        "Статистика за последний час. Остановить слежение: /route_stop. "
        f"Местное время - {eve_time()} ET"
    )
