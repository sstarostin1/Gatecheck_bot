"""Конфигурация Gatecheck Bot: чтение .env и валидация настроек."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Корень репозитория = родитель пакета gatecheck_bot/
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Ошибка конфигурации: отсутствуют или неверны обязательные переменные."""


@dataclass(frozen=True)
class Settings:
    """Иммутабельные настройки приложения."""

    bot_token: str
    admin_ids: frozenset[int]

    @classmethod
    def from_env(cls) -> Settings:
        """Собрать Settings из окружения/.env. Бросает ConfigError при проблемах."""
        token = (os.getenv("BOT_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "BOT_TOKEN не задан. Скопируй .env.example в .env "
                "и укажи токен от @BotFather."
            )

        raw_admin = (os.getenv("ADMIN_IDS") or "").replace(";", ",").replace(" ", ",")
        admin_ids = frozenset(
            int(part) for part in raw_admin.split(",") if part.strip().isdigit()
        )
        return cls(bot_token=token, admin_ids=admin_ids)
