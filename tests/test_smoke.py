"""Смоук-тесты скелета: импорт, конфиг, сборка роутера."""

import pytest

import gatecheck_bot
from gatecheck_bot.config import ConfigError, Settings


def test_package_version() -> None:
    assert gatecheck_bot.__version__


def test_settings_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_settings_parses_admin_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "12345:TEST_TOKEN")
    monkeypatch.setenv("ADMIN_IDS", "111, 222; 333 abc")
    settings = Settings.from_env()
    assert settings.bot_token == "12345:TEST_TOKEN"
    assert settings.admin_ids == frozenset({111, 222, 333})


def test_handlers_build_router() -> None:
    from gatecheck_bot.handlers import build_router

    settings = Settings(bot_token="12345:TEST_TOKEN", admin_ids=frozenset({1}))
    assert build_router(settings) is not None
