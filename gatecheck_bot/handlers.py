"""Хэндлеры v0.10 — по спеке docs/MESSAGES.md (§1–§14).

HTML parse mode (разметка <b>/<i>/<pre>/<blockquote expandable>), все данные — esc().
Команды с подчёркиванием (/zone_on, /zone_status, /route_stop — §0.2); старые формы
с пробелом поддерживаются для совместимости. Кнопки reply-меню обрабатываются по точному
тексту (§13). /ping — только админам, не-админам бот молчит (§12).
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from . import __version__
from .config import BASE_DIR, Settings
from .monitoring import RouteMonitor, format_isk, route_system_block, route_verdict, verdict_line
from .render import bold, esc, italic, pre, route_footer, systems_word
from .routing import (
    Graph,
    build_name_index,
    find_route,
    format_route,
    load_graph,
    resolve_system,
)
from .storage import Storage
from .ui import (
    BTN_ROUTE_STATUS,
    BTN_ROUTE_STOP,
    BTN_SETTINGS,
    BTN_ZONE_OFF,
    BTN_ZONE_ON,
    REFRESH_TEXT,
    main_keyboard,
)
from .zone import SETTING_BOUNDS, ZoneMonitor, clamp_setting, parse_setting_value

PROCESS_STARTED_MONOTONIC = time.monotonic()

# Ошибки и подсказки (§14)
ERR_NO_GRAPH = (
    "Граф гейтов ещё не собран: python scripts/fetch_static.py"
)
ERR_NO_GATES = "data/gates.json не собран — слежение недоступно."
ERR_ROUTE_ACTIVE = "Один маршрут уже активен — останови мониторинг предыдущего и создай новый."


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


_GATES: tuple[dict[str, set[int]], dict[int, str], dict[int, str]] | None = None


def _get_gates() -> tuple[dict[str, set[int]], dict[int, str], dict[int, str]]:
    """Индексы из data/gates.json: system_id → гейты, itemID → имя, itemID → dest system_id."""
    global _GATES
    if _GATES is None:
        try:
            doc = json.loads((BASE_DIR / "data" / "gates.json").read_text(encoding="utf-8"))
            by_system: dict[str, set[int]] = {}
            names: dict[int, str] = {}
            dests: dict[int, str] = {}
            for gid, gate in (doc.get("gates") or {}).items():
                by_system.setdefault(str(gate.get("system_id")), set()).add(int(gid))
                names[int(gid)] = str(gate.get("name", ""))
                if gate.get("dest_system_id"):
                    dests[int(gid)] = str(gate["dest_system_id"])
            _GATES = (by_system, names, dests)
        except (OSError, ValueError):
            _GATES = ({}, {}, {})
    return _GATES


def build_route_reply(graph: Graph, query: str) -> tuple[str, list[str] | None]:
    """Текст ответа на /route (HTML) + список system_id (None — маршрута/разбора нет)."""
    parts = [part for part in re.split(r"\s*(?:→|->|;|,)\s*|\s+", query.strip()) if part]
    if not parts:
        return "Формат: /route Amamake Jita (разделитель — пробел, → или запятая).", None
    if len(parts) != 2:
        return "Нужно ровно две системы: /route Amamake Jita.", None
    index = build_name_index(graph)
    start = resolve_system(parts[0], index)
    goal = resolve_system(parts[1], index)
    missing = [name for name, sid in ((parts[0], start), (parts[1], goal)) if sid is None]
    if missing:
        return (
            f"Не нашёл систему: {esc(', '.join(missing))}. "
            "Проверь написание (граф — весь Новый Эден, без вормхолов)."
        ), None
    if start == goal:
        return f"{esc(graph.name_of(start or ''))}: ты уже там 🙂", None
    route = find_route(graph.adjacency, start or "", goal or "")
    if route is None:
        return (
            f"Маршрут {esc(graph.name_of(start or ''))} → {esc(graph.name_of(goal or ''))} "
            "не найден (нет гейт-связности)."
        ), None
    header = (
        f"🛰 {esc(format_route(graph, route))}\n"
        f"Прыжков: {len(route) - 1}"
    )
    return header, route


def route_systems_block(graph: Graph, route: list[str]) -> str:
    """Моноширинный список систем маршрута для EVE Notepad (§6.2): «Amamake 0.4»."""
    lines = []
    for sid in route:
        name = esc(graph.name_of(sid))
        sec = float((graph.systems.get(sid) or {}).get("security_status") or 0.0)
        lines.append(f"{name} {sec:.1f}")
    return pre("\n".join(lines))


async def compose_route_start(
    monitor: RouteMonitor, watch, route: list[str], graph: Graph
) -> str:
    """Сообщение старта маршрута (§6): вердикт → список систем → первый опрос → футер."""
    now_epoch = datetime.now(UTC).timestamp()
    level, levels = route_verdict(watch, now_epoch)
    total = len(route)
    head = bold(
        f"🛡 Маршрут {esc(graph.name_of(route[0]))} → {esc(graph.name_of(route[-1]))} проложен: "
        f"{total} {systems_word(total)}, "
        f"слежение на {int(monitor.ttl_seconds / 60)} мин активировано."
    )
    lines = [head, "", verdict_line(level, levels)]
    if level != 0:
        lines.append(
            esc(
                "(Не исключает бабблов по пути варпа внутри системы и внезапных атак, "
                "вердикт не абсолютен)"
            )
        )
    lines += [
        "",
        bold("Список систем для копирования и разметки в утилиту Notepad:"),
        route_systems_block(graph, route),
    ]
    if level == 0:
        lines += [
            bold("Сейчас гайки на маршруте не кемпят."),
            f"Точек проверено: {total}.",
        ]
    else:
        ship_ids: set[int] = set()
        burning: list[str] = []
        for sid in route:
            hour = watch.hour_events(sid, now_epoch)
            if hour:
                burning.append(sid)
                ship_ids.update(tid for e in hour for tid in e["att_ships"])
        ship_names = await monitor._resolve_ship_names(ship_ids)
        lines += ["", bold("Кемпы по пути:"), ""]
        for sid in sorted(burning, key=route.index):
            lines.append(route_system_block(watch, sid, now_epoch, ship_names))
            lines.append("")
        clean = total - len(burning)
        lines.append(f"✅ Чисто: {clean}/{total} {systems_word(total)}")
    lines += ["", route_footer()]
    return "\n".join(lines)


def build_router(
    settings: Settings,
    monitor: RouteMonitor | None = None,
    zone_monitor: ZoneMonitor | None = None,
    storage: Storage | None = None,
) -> Router:
    """Собрать роутер v0.10 (мониторы/хранилище — общие на процесс)."""
    router = Router(name="basic")
    monitor = monitor or RouteMonitor()
    zone_monitor = zone_monitor or ZoneMonitor()

    def user_thresholds(chat_id: int) -> dict | None:
        """Пользовательские пороги D5 из Storage (None — дефолты)."""
        if storage is None:
            return None
        user_settings = storage.get_settings(chat_id)
        return user_settings or None

    def _gates_ready() -> bool:
        gate_index, _, _ = _get_gates()
        return bool(gate_index)

    async def zone_on_action(message: Message) -> None:
        graph = _get_graph()
        if graph is None:
            await message.answer(esc(ERR_NO_GRAPH))
            return
        if not _gates_ready():
            await message.answer(esc(ERR_NO_GATES))
            return
        gate_index, gate_names, gate_labels = _get_gates()
        watch = zone_monitor.start(
            message.chat.id, graph, gate_index, gate_names, gate_labels,
            thresholds=user_thresholds(message.chat.id),
        )
        current = user_thresholds(message.chat.id) or {}
        hour = int(current.get("hour", zone_monitor.hour_threshold))
        isk = format_isk(float(current.get("isk", zone_monitor.isk)))
        await message.answer(
            f"🔥 Зона включена: «Hed + соседи», {len(watch.systems)} "
            f"{systems_word(len(watch.systems))}, опрос каждые {int(zone_monitor.poll_interval)} с.\n"
            f"Алерты: ≥{hour} киллов на гейтах за час или droppable ≥{esc(isk)} за час.\n"
            "/zone_status — состояние · /zone_off — выключить."
        )

    async def zone_off_action(message: Message) -> None:
        await message.answer(zone_monitor.stop(message.chat.id))

    async def zone_status_action(message: Message) -> None:
        graph = _get_graph()
        if message.chat.id in zone_monitor.watches:
            await message.answer(await zone_monitor.status(message.chat.id))
            return
        if graph is None:
            await message.answer(esc(ERR_NO_GRAPH))
            return
        if not _gates_ready():
            await message.answer(esc(ERR_NO_GATES))
            return
        gate_index, gate_names, gate_labels = _get_gates()
        # Без подписки — разовый живой опрос зоны прямо сейчас (§4).
        await message.answer(
            await zone_monitor.live_snapshot(graph, gate_index, gate_names, gate_labels)
        )

    async def route_stop_action(message: Message) -> None:
        await message.answer(monitor.stop(message.chat.id))

    async def route_status_action(message: Message) -> None:
        if message.chat.id not in monitor.watches:
            await message.answer("Слежение не активно. /route Amamake Jita — включить.")
            return
        await message.answer(await monitor.status(message.chat.id))

    async def settings_view(message: Message) -> None:
        current = storage.get_settings(message.chat.id) if storage else {}
        hour = int(current.get("hour", 8))
        isk = format_isk(float(current.get("isk", 5e8)))
        cooldown_min = int(float(current.get("cooldown", 900)) / 60)
        attackers = "вкл" if current.get("attackers", True) else "выкл"
        low_h, high_h = SETTING_BOUNDS["hour"]
        low_i, high_i = SETTING_BOUNDS["isk"]
        low_c, high_c = SETTING_BOUNDS["cooldown"]
        await message.answer(
            bold("Пороги зоны (D5), границы:") + "\n"
            f"• hour: {hour} киллов на гейтах за час (пределы {low_h:g}…{high_h:g})\n"
            f"• isk: {esc(isk)} droppable на гейтах за час (пределы {esc(format_isk(low_i))}…"
            f"{esc(format_isk(high_i))})\n"
            f"• cooldown: {cooldown_min} мин (пределы {int(low_c / 60)}…{int(high_c / 60)} мин) "
            "— только для зоны\n"
            f"• атака в алертах: {attackers} — сообщение о составе и виде кемпа\n\n"
            + italic(
                "Изменить: /settings_hour 10 · /settings_isk 0.7B · /settings_cooldown 20м · "
                "/settings_attackers off"
            )
            + "\n"
            + italic("Сброс всех: /settings_reset")
        )


    # --- базовые команды ---------------------------------------------------

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        name = message.from_user.first_name if message.from_user else "пилот"
        await message.answer(
            f"Привет, {esc(name)}! 👋\n\n"
            f"Gatecheck Bot v{esc(__version__)} — слежение за гейт-кампами EVE Online "
            "для дальнобойщиков и сальважеров. Вдохновлен небезызвестным "
            '[сервисом](https://eve-gatecheck.space/) и адаптирован под специфические нужды.\n\n'
            "Основные команды:\n"
            "• /zone_on — включить мониторинг зоны «Hed + соседи» для сальважа (алерты по гейтам)\n"
            "• /zone_status — разовый отчёт по гейтам зоны сальважа\n"
            "• /route Amamake Jita — проложить маршрут + начать слежение (TTL 1 ч)\n"
            "• /help — вся справка\n\n"
            'Подписок и доната нет, но если бот понравился - "спасибо" любыми исками на ник '
            "<code>Grastanideus</code> :-)",
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            bold("Команды:") + "\n"
            "/zone_on — включить зону «Hed + соседи в радиусе одной системы» · "
            "/zone_off — выключить\n"
            "/zone_status — одноразовый отчёт по гейтам зоны, актуальный по активным кемпам\n"
            "/route {A} {B} — кратчайший маршрут между произвольными системами + частый "
            "мониторинг систем по дороге (на протяжении часа, либо до отмены)\n"
            "/route_status — апрос статуса горящих систем по дороге (если в алерты не верится)\n"
            "/route_stop — остановить мониторинг (рекомендуется по окончании рейса: "
            "не нагружайте бота)\n"
            "/settings — пороги алертов\n\n"
            "Кнопки: 🔄 Гейты: обновить · 🛡 Зона вкл/выкл · 🚀 Маршрут · ⚙️ Пороги.\n"
            "Все отчёты — только по киллам на гейтах, не далее чем за последний час.\n"
            f"Версия: v{esc(__version__)}, "
            '[Гитхаб-репозиторий](https://github.com/sstarostin1/Gatecheck_bot).',
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )

    @router.message(Command("ping"))
    async def cmd_ping(message: Message) -> None:
        if not is_admin(settings, message):
            return  # §12: не-админ — бот молчит
        graph = _get_graph()
        if graph is not None:
            counts_meta = graph.meta.get("counts", {})
            statics = (
                f"{counts_meta.get('systems')} систем / {counts_meta.get('gates')} гейтов, "
                f"от {esc(str(graph.meta.get('generated_at', '?'))[:10])}"
            )
        else:
            statics = "граф не собран"
        uptime_min = int((time.monotonic() - PROCESS_STARTED_MONOTONIC) / 60)
        requests = monitor.stat_requests + zone_monitor.stat_requests
        errors = monitor.stat_errors + zone_monitor.stat_errors
        await message.answer(
            f"pong ✅ v{esc(__version__)}\n"
            f"Аптайм: {uptime_min} мин\n"
            f"Статика: {statics}\n"
            f"Слежки: зон {len(zone_monitor.watches)}, маршрутов {len(monitor.watches)}\n"
            f"zK: запросов {requests}, ошибок {errors}"
        )


    # --- настройки (§11) ---------------------------------------------------

    def _settings_reply(chat_id: int, key: str, value: float | bool) -> str:
        if key == "attackers":
            state = "вкл" if value else "выкл"
            return f"✅ атака в алертах = {state} (действует со следующего /zone_on)"
        if key == "hour":
            pretty = f"{int(value)}"
        elif key == "isk":
            pretty = format_isk(value)
        else:
            pretty = f"{int(value / 60)} мин"
        return f"✅ {key} = {pretty} (действует со следующего /zone_on)"

    def _apply_setting(chat_id: int, key: str, value: float | bool) -> str:
        if storage is None:
            return "Хранилище недоступно — пороги не сохраняются."
        user_settings = storage.get_settings(chat_id)
        if key == "attackers":
            user_settings["attackers"] = bool(value)
        else:
            clamped = clamp_setting(key, float(value))
            user_settings[key] = clamped
            value = clamped
        storage.set_settings(chat_id, user_settings)
        return _settings_reply(chat_id, key, value)

    @router.message(Command("settings"))
    async def cmd_settings(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip()
        if not args:
            await settings_view(message)
            return
        # Совместимость: /settings hour 10 · /settings isk 0.7B · /settings attackers off
        parts = args.split()
        if len(parts) == 2 and parts[0] in SETTING_BOUNDS:
            value = parse_setting_value(parts[0], parts[1])
            if value is None:
                await message.answer(f"Не разобрал значение «{esc(parts[1])}».")
                return
            await message.answer(_apply_setting(message.chat.id, parts[0], value))
            return
        if len(parts) == 2 and parts[0] == "attackers":
            state = parts[1].lower() in {"on", "вкл", "1"}
            await message.answer(_apply_setting(message.chat.id, "attackers", state))
            return
        await message.answer(
            "Формат: /settings_hour 10 · /settings_isk 0.7B · /settings_cooldown 20м · "
            "/settings_attackers off · /settings_reset"
        )

    @router.message(Command("settings_reset"))
    async def cmd_settings_reset(message: Message) -> None:
        if storage is not None:
            storage.set_settings(message.chat.id, {})
        await message.answer("Пороги сброшены к дефолтам — действуют со следующего /zone_on.")

    @router.message(Command("settings_hour"))
    async def cmd_settings_hour(message: Message, command: CommandObject) -> None:
        value = parse_setting_value("hour", (command.args or "").strip())
        if value is None:
            await message.answer("Формат: /settings_hour 10 (пределы 4…20).")
            return
        await message.answer(_apply_setting(message.chat.id, "hour", value))

    @router.message(Command("settings_isk"))
    async def cmd_settings_isk(message: Message, command: CommandObject) -> None:
        value = parse_setting_value("isk", (command.args or "").strip())
        if value is None:
            await message.answer("Формат: /settings_isk 0.7B (пределы 10M…5B).")
            return
        await message.answer(_apply_setting(message.chat.id, "isk", value))

    @router.message(Command("settings_cooldown"))
    async def cmd_settings_cooldown(message: Message, command: CommandObject) -> None:
        value = parse_setting_value("cooldown", (command.args or "").strip())
        if value is None:
            await message.answer("Формат: /settings_cooldown 20м (пределы 5…60 мин).")
            return
        await message.answer(_apply_setting(message.chat.id, "cooldown", value))

    @router.message(Command("settings_attackers"))
    async def cmd_settings_attackers(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip().lower()
        if raw in {"on", "вкл", "включить", "1"}:
            state = True
        elif raw in {"off", "выкл", "выключить", "0"}:
            state = False
        else:
            await message.answer("Формат: /settings_attackers on|off.")
            return
        await message.answer(_apply_setting(message.chat.id, "attackers", state))


    # --- маршрут (§6–§8) ----------------------------------------------------

    @router.message(Command("route"))
    async def cmd_route(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip()
        if args.lower() in {"stop", "стоп"}:
            await route_stop_action(message)
            return
        if args.lower() in {"status", "статус"}:
            await route_status_action(message)
            return

        graph = _get_graph()
        if graph is None:
            await message.answer(esc(ERR_NO_GRAPH))
            return
        if not _gates_ready():
            await message.answer(esc(ERR_NO_GATES))
            return
        text, route = build_route_reply(graph, args)
        if route is None:
            await message.answer(text)
            return
        if message.chat.id in monitor.watches:
            await message.answer(ERR_ROUTE_ACTIVE)
            return

        gate_index, gate_names, gate_dest = _get_gates()
        watch = monitor.begin(message.chat.id, route, graph, gate_index, gate_names, gate_dest)
        # §6.3: первый опрос сразу — вердикт и блоки в том же сообщении, без алертов.
        await monitor.snapshot(watch)
        await message.answer(await compose_route_start(monitor, watch, route, graph))

    @router.message(Command("route_status"))
    async def cmd_route_status(message: Message) -> None:
        await route_status_action(message)

    @router.message(Command("route_stop"))
    async def cmd_route_stop(message: Message) -> None:
        await route_stop_action(message)

    # --- зона (§1–§4) -------------------------------------------------------

    @router.message(Command("zone_on"))
    async def cmd_zone_on(message: Message) -> None:
        await zone_on_action(message)

    @router.message(Command("zone_off"))
    async def cmd_zone_off(message: Message) -> None:
        await zone_off_action(message)

    @router.message(Command("zone_status"))
    async def cmd_zone_status(message: Message) -> None:
        await zone_status_action(message)

    # Совместимость: /zone on|off|status (старые формы с пробелом, §0.2)
    @router.message(Command("zone"))
    async def cmd_zone_legacy(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip().lower()
        if args in {"on", "вкл", "включить"}:
            await zone_on_action(message)
        elif args in {"off", "выкл", "выключить"}:
            await zone_off_action(message)
        elif args in {"status", "статус"}:
            await zone_status_action(message)
        else:
            await message.answer(
                "Формат: /zone_on — включить · /zone_status — состояние · /zone_off — выключить"
            )

    # --- кнопки меню (§13): обработка по точному тексту ---------------------

    @router.message(F.text == REFRESH_TEXT)
    async def btn_refresh(message: Message) -> None:
        """«🔄 Гейты: обновить» — тик ТОЛЬКО зональных слежек + отчёт §3/§4 (§5)."""
        if message.chat.id in zone_monitor.watches:
            await zone_monitor.force_tick(message.chat.id, message.bot)
            await message.answer(await zone_monitor.status(message.chat.id))
            return
        graph = _get_graph()
        if graph is None:
            await message.answer(esc(ERR_NO_GRAPH))
            return
        if not _gates_ready():
            await message.answer(esc(ERR_NO_GATES))
            return
        gate_index, gate_names, gate_labels = _get_gates()
        await message.answer(
            await zone_monitor.live_snapshot(graph, gate_index, gate_names, gate_labels)
        )

    @router.message(F.text == BTN_ZONE_ON)
    async def btn_zone_on(message: Message) -> None:
        await zone_on_action(message)

    @router.message(F.text == BTN_ZONE_OFF)
    async def btn_zone_off(message: Message) -> None:
        await zone_off_action(message)

    @router.message(F.text == BTN_ROUTE_STATUS)
    async def btn_route_status(message: Message) -> None:
        await route_status_action(message)

    @router.message(F.text == BTN_ROUTE_STOP)
    async def btn_route_stop(message: Message) -> None:
        await route_stop_action(message)

    @router.message(F.text == BTN_SETTINGS)
    async def btn_settings(message: Message) -> None:
        await settings_view(message)

    # Inline «🔄 Обновить» — только на зональных репортах (§13).
    @router.callback_query(F.data == "refresh:zone")
    async def cb_refresh_zone(callback: CallbackQuery) -> None:
        await callback.answer("Обновляю…")
        if callback.message is None:
            return
        chat_id = callback.message.chat.id
        if chat_id in zone_monitor.watches:
            await zone_monitor.force_tick(chat_id, callback.bot)
            await callback.message.answer(await zone_monitor.status(chat_id))
            return
        graph = _get_graph()
        if graph is None:
            await callback.message.answer(esc(ERR_NO_GRAPH))
            return
        gate_index, gate_names, gate_labels = _get_gates()
        if not gate_index:
            await callback.message.answer(esc(ERR_NO_GATES))
            return
        await callback.message.answer(
            await zone_monitor.live_snapshot(graph, gate_index, gate_names, gate_labels)
        )

    # Эхо на любой текст — проверка приёма/отправки (скелет).
    @router.message(F.text)
    async def echo(message: Message) -> None:
        await message.answer(f"✅ Получил: «{esc(message.text or '')}»")

    # Всё, что не текст (фото, стикеры и т.п.) — подтверждаем приёмом.
    @router.message()
    async def catch_all(message: Message) -> None:
        await message.answer("✅ Получил не-текстовое сообщение (скелет его просто игнорирует).")

    return router
