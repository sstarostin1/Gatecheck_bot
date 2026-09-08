"""Конфигурация Gatecheck Bot: чтение .env и валидация настроек."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .transport.proxy_types import DEFAULT_SOURCES

# Корень репозитория = родитель пакета gatecheck_bot/
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Ошибка конфигурации: отсутствуют или неверны обязательные переменные."""


def _get_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _get_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    return int(_get_float(name, default))


@dataclass(frozen=True)
class Settings:
    """Иммутабельные настройки приложения."""

    bot_token: str
    admin_ids: frozenset[int]
    # Транспорт Telegram: статичный прокси и/или автономный пул.
    proxy_url: str | None = None           # статичный прокси (наивысший приоритет)
    proxy_autopool: bool = True            # авто-добыча/проверка/ротация прокси
    proxy_sources: tuple[str, ...] = DEFAULT_SOURCES
    proxy_manual: tuple[str, ...] = ()     # свои: socks5://... | http://... | vless://...
    proxy_check_timeout: float = 8.0       # таймаут пробы одного прокси, сек
    proxy_pool_size: int = 20              # сколько рабочих прокси держать
    proxy_refresh_sec: int = 1800          # резерв: период фоновой проверки (M2+)
    proxy_cache_path: str = "data/proxy_cache.json"

    @classmethod
    def from_env(cls, require_token: bool = True) -> Settings:
        """Собрать Settings из окружения/.env. Бросает ConfigError при проблемах."""
        token = (os.getenv("BOT_TOKEN") or "").strip()
        if require_token and not token:
            raise ConfigError(
                "BOT_TOKEN не задан. Скопируй .env.example в .env "
                "и укажи токен от @BotFather."
            )

        raw_admin = (os.getenv("ADMIN_IDS") or "").replace(";", ",").replace(" ", ",")
        admin_ids = frozenset(
            int(part) for part in raw_admin.split(",") if part.strip().isdigit()
        )
        proxy_url = (os.getenv("PROXY_URL") or "").strip() or None
        manual = tuple(
            line.strip()
            for line in (os.getenv("PROXY_MANUAL") or "").split("|")
            if line.strip()
        )
        sources = tuple(
            url.strip() for url in (os.getenv("PROXY_SOURCES") or "").split(",") if url.strip()
        ) or DEFAULT_SOURCES
        return cls(
            bot_token=token,
            admin_ids=admin_ids,
            proxy_url=proxy_url,
            proxy_autopool=_get_bool("PROXY_AUTOPOOL", True),
            proxy_sources=sources,
            proxy_manual=manual,
            proxy_check_timeout=_get_float("PROXY_CHECK_TIMEOUT", 8.0),
            proxy_pool_size=_get_int("PROXY_POOL_SIZE", 20),
            proxy_refresh_sec=_get_int("PROXY_REFRESH_SEC", 1800),
            proxy_cache_path=(os.getenv("PROXY_CACHE") or "data/proxy_cache.json").strip(),
        )
