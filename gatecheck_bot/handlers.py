"""Базовые хэндлеры скелета: /start, /help, /ping и эхо.

Задача скелета — доказать, что бот получает сообщения от пользователей
и успешно отправляет ответы (long polling, без webhook).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from . import __version__
from .config import Settings


def is_admin(settings: Settings, message: Message) -> bool:
    """Проверить, что автор сообщения — администратор (из ADMIN_IDS)."""
    return bool(message.from_user and message.from_user.id in settings.admin_ids)


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

    # Эхо на любой текст — основная проверка приёма/отправки на этом этапе.
    @router.message(F.text)
    async def echo(message: Message) -> None:
        await message.answer(f"✅ Получил: «{message.text}»")

    # Всё, что не текст (фото, стикеры и т.п.) — тоже подтверждаем приёмом.
    @router.message()
    async def catch_all(message: Message) -> None:
        await message.answer("✅ Получил не-текстовое сообщение (скелет его просто игнорирует).")

    return router
