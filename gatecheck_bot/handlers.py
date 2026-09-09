"""Базовые хэндлеры скелета: /start, /help, /ping, /route и эхо.

Задача скелета — доказать, что бот получает сообщения от пользователей
и успешно отправляет ответы (long polling, без webhook).
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from . import __version__
from .config import BASE_DIR, Settings
from .routing import (
    Graph,
    build_name_index,
    find_route,
    format_route,
    load_graph,
    resolve_system,
)


def is_admin(settings: Settings, message: Message) -> bool:
    """Проверить, что автор сообщения — администратор (из ADMIN_IDS)."""
    return bool(message.from_user and message.from_user.id in settings.admin_ids)


# Граф грузится лениво; пока data/graph.json не появился — каждая /route пытается снова
# (можно собрать граф scripts/fetch_static.py на живом боту — рестарт не нужен).
_GRAPH: Graph | None = None


def _get_graph() -> Graph | None:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = load_graph(BASE_DIR / "data" / "graph.json")
    return _GRAPH


def build_route_reply(graph: Graph, query: str) -> str:
    """Текст ответа на /route: парсинг аргументов, резолв имён, BFS, формат."""
    parts = [
        part for part in re.split(r"\s*(?:→|->|;|,)\s*|\s+", query.strip()) if part
    ]
    if not parts:
        return "Формат: /route Amamake Siseide (разделитель — пробел, → или запятая)."
    if len(parts) != 2:
        return "Нужно ровно две системы: /route Amamake Siseide."
    index = build_name_index(graph)
    start = resolve_system(parts[0], index)
    goal = resolve_system(parts[1], index)
    missing = [
        name for name, sid in ((parts[0], start), (parts[1], goal)) if sid is None
    ]
    if missing:
        return (
            f"Не нашёл систему: {', '.join(missing)}. "
            "Проверь написание (граф региона — Heimatar)."
        )
    if start == goal:
        return f"{graph.name_of(start or '')}: ты уже там 🙂"
    route = find_route(graph.adjacency, start or "", goal or "")
    if route is None:
        return (
            f"Маршрут {graph.name_of(start or '')} → {graph.name_of(goal or '')} "
            "не найден (в пределах графа региона)."
        )
    return f"🛰 {format_route(graph, route)}\nПрыжков: {len(route) - 1}"


def build_router(settings: Settings) -> Router:
    """Собрать роутер базовых хэндлеров."""
    router = Router(name="basic")

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        name = message.from_user.first_name if message.from_user else "пилот"
        await message.answer(
            f"Привет, {name}! 👋\n\n"
            f"Это Gatecheck Bot v{__version__} (скелет).\n\n"
            "Умеет сейчас:\n"
            "• /help — справка\n"
            "• /ping — проверка живости (для админов)\n"
            "• эхо — вернёт любой твой текст обратно\n\n"
            "Мониторинг гейтов EVE Online будет добавлен в следующих версиях "
            "(план — docs/VISION.md)."
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "Команды:\n"
            "/start — приветствие\n"
            "/help — эта справка\n"
            "/ping — живость бота (админ)\n\n"
            f"Версия: v{__version__} (скелет)."
        )

    @router.message(Command("ping"))
    async def cmd_ping(message: Message) -> None:
        if is_admin(settings, message):
            await message.answer(f"pong ✅ (v{__version__})")
        else:
            await message.answer("pong ✅ (режим скелета: без админ-статуса)")

    # /route A B — кратчайший маршрут по классическим гейтам (BFS, M1).
    @router.message(Command("route"))
    async def cmd_route(message: Message, command: CommandObject) -> None:
        graph = _get_graph()
        if graph is None:
            await message.answer(
                "Граф гейтов ещё не собран. Один раз на хосте выполни:\n"
                "python scripts/fetch_static.py\n…и попробуй снова."
            )
            return
        await message.answer(build_route_reply(graph, command.args or ""))

    # Эхо на любой текст — основная проверка приёма/отправки на этом этапе.
    @router.message(F.text)
    async def echo(message: Message) -> None:
        await message.answer(f"✅ Получил: «{message.text}»")

    # Всё, что не текст (фото, стикеры и т.п.) — тоже подтверждаем приёмом.
    @router.message()
    async def catch_all(message: Message) -> None:
        await message.answer("✅ Получил не-текстовое сообщение (скелет его просто игнорирует).")

    return router
