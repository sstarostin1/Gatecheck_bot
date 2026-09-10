"""Базовые хэндлеры скелета: /start, /help, /ping, /route и эхо.

Задача скелета — доказать, что бот получает сообщения от пользователей
и успешно отправляет ответы (long polling, без webhook).
"""

from __future__ import annotations

import json
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from . import __version__
from .config import BASE_DIR, Settings
from .monitoring import RouteMonitor
from .routing import (
    Graph,
    build_name_index,
    find_route,
    format_route,
    load_graph,
    resolve_system,
)
from .zone import ZoneMonitor


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


_GATES: tuple[dict[str, set[int]], dict[int, str]] | None = None


def _get_gates() -> tuple[dict[str, set[int]], dict[int, str]]:
    """Индексы из data/gates.json: system_id → itemID гейтов и itemID → имя."""
    global _GATES
    if _GATES is None:
        try:
            doc = json.loads((BASE_DIR / "data" / "gates.json").read_text(encoding="utf-8"))
            by_system: dict[str, set[int]] = {}
            names: dict[int, str] = {}
            for gid, gate in (doc.get("gates") or {}).items():
                by_system.setdefault(str(gate.get("system_id")), set()).add(int(gid))
                names[int(gid)] = str(gate.get("name", ""))
            _GATES = (by_system, names)
        except (OSError, ValueError):
            _GATES = ({}, {})
    return _GATES


def build_route_reply(graph: Graph, query: str) -> tuple[str, list[str] | None]:
    """Текст ответа на /route + список system_id (None, если маршрута нет)."""
    parts = [
        part for part in re.split(r"\s*(?:→|->|;|,)\s*|\s+", query.strip()) if part
    ]
    if not parts:
        return "Формат: /route Amamake Siseide (разделитель — пробел, → или запятая).", None
    if len(parts) != 2:
        return "Нужно ровно две системы: /route Amamake Siseide.", None
    index = build_name_index(graph)
    start = resolve_system(parts[0], index)
    goal = resolve_system(parts[1], index)
    missing = [
        name for name, sid in ((parts[0], start), (parts[1], goal)) if sid is None
    ]
    if missing:
        return (
            f"Не нашёл систему: {', '.join(missing)}. "
            "Проверь написание (граф — весь Новый Эден, без вормхолов)."
        ), None
    if start == goal:
        return f"{graph.name_of(start or '')}: ты уже там 🙂", [start]
    route = find_route(graph.adjacency, start or "", goal or "")
    if route is None:
        return (
            f"Маршрут {graph.name_of(start or '')} → {graph.name_of(goal or '')} "
            "не найден (нет гейт-связности)."
        ), None
    return f"🛰 {format_route(graph, route)}\nПрыжков: {len(route) - 1}", route


def build_router(
    settings: Settings,
    monitor: RouteMonitor | None = None,
    zone_monitor: ZoneMonitor | None = None,
) -> Router:
    """Собрать роутер базовых хэндлеров (мониторы — общие на жизнь процесса)."""
    router = Router(name="basic")
    monitor = monitor or RouteMonitor()
    zone_monitor = zone_monitor or ZoneMonitor()

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
            "/ping — живость бота (админ)\n"
            "/route A B — маршрут по гейтам + слежение кемпов (TTL 1 ч)\n"
            "/route status — статистика · /route stop — выключить\n"
            "/zone on|status|off — мониторинг зоны фарма «Hed + соседи»\n\n"
            f"Версия: v{__version__}."
        )

    @router.message(Command("ping"))
    async def cmd_ping(message: Message) -> None:
        if is_admin(settings, message):
            await message.answer(f"pong ✅ (v{__version__})")
        else:
            await message.answer("pong ✅ (режим скелета: без админ-статуса)")

    # /route A B — маршрут + слежение гейтов (BFS + poller zK, M1/M3).
    @router.message(Command("route"))
    async def cmd_route(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip()
        if args.lower() in {"stop", "стоп"}:
            await message.answer(monitor.stop(message.chat.id))
            return
        if args.lower() in {"status", "статус"}:
            await message.answer(monitor.status(message.chat.id))
            return

        graph = _get_graph()
        if graph is None:
            await message.answer(
                "Граф гейтов ещё не собран. Один раз на хосте выполни:\n"
                "python scripts/fetch_static.py\n…и попробуй снова."
            )
            return
        text, route = build_route_reply(graph, args)
        if route is None:
            await message.answer(text)
            return

        gate_index, gate_names = _get_gates()
        if not gate_index:
            await message.answer(
                text + "\n\n⚠️ data/gates.json не собран — слежение недоступно, только маршрут."
            )
            return
        monitor.start(message.chat.id, route, graph, gate_index, gate_names)
        await message.answer(
            f"{text}\n\n"
            f"🛡 Слежение гейтов маршрута включено (TTL 60 мин, опрос каждые "
            f"{int(monitor.poll_interval)} с). Новые киллы на гейтах маршрута — пришлю алерт.\n"
            "/route status — статистика · /route stop — выключить."
        )

    # /zone on|off|status — фоновый мониторинг зоны фарма (M2, пресет «Hed + соседи»).
    @router.message(Command("zone"))
    async def cmd_zone(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip().lower()
        if args in {"on", "вкл", "включить"}:
            graph = _get_graph()
            if graph is None:
                await message.answer(
                    "Граф гейтов ещё не собран. Один раз на хосте выполни:\n"
                    "python scripts/fetch_static.py\n…и попробуй снова."
                )
                return
            gate_index, gate_names = _get_gates()
            if not gate_index:
                await message.answer("data/gates.json не собран — зона недоступна.")
                return
            watch = zone_monitor.start(message.chat.id, graph, gate_index, gate_names)
            zone_names = ", ".join(watch.names[sid] for sid in watch.systems)
            await message.answer(
                f"🔥 Зона включена: «Hed + соседи», {len(watch.systems)} систем, "
                f"опрос каждые {int(zone_monitor.poll_interval)} с.\n"
                f"Состав: {zone_names}\n\n"
                "Алерты по гейтам зоны: всплеск ≥3 килла/10 мин, накопление ≥8/час, "
                "дорогой droppable ISK.\n"
                "/zone status — состояние · /zone off — выключить."
            )
        elif args in {"off", "выкл", "выключить"}:
            await message.answer(zone_monitor.stop(message.chat.id))
        elif args in {"status", "статус"}:
            await message.answer(zone_monitor.status(message.chat.id))
        else:
            await message.answer(
                "Формат:\n"
                "/zone on — включить зону «Hed + соседи»\n"
                "/zone status — состояние зоны\n"
                "/zone off — выключить"
            )

    # Эхо на любой текст — основная проверка приёма/отправки на этом этапе.
    @router.message(F.text)
    async def echo(message: Message) -> None:
        await message.answer(f"✅ Получил: «{message.text}»")

    # Всё, что не текст (фото, стикеры и т.п.) — тоже подтверждаем приёмом.
    @router.message()
    async def catch_all(message: Message) -> None:
        await message.answer("✅ Получил не-текстовое сообщение (скелет его просто игнорирует).")

    return router
