"""Точка входа Gatecheck Bot: запуск long polling через aiogram 3."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from . import __version__
from .config import ConfigError, Settings
from .handlers import build_router

logger = logging.getLogger("gatecheck_bot")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


async def run(settings: Settings) -> None:
    """Поднять Bot+Dispatcher и крутить long polling до прерывания."""
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(build_router(settings))

    me = await bot.get_me()
    logger.info("Gatecheck Bot v%s запущен как @%s (long polling)", __version__, me.username)
    await dp.start_polling(bot)


def main() -> None:
    setup_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logger.error("Конфигурация: %s", exc)
        raise SystemExit(2) from None

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C).")


if __name__ == "__main__":
    main()
