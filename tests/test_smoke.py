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


def test_settings_parses_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "12345:TEST_TOKEN")
    monkeypatch.setenv("PROXY_URL", " socks5://127.0.0.1:1080 ")
    settings = Settings.from_env()
    assert settings.proxy_url == "socks5://127.0.0.1:1080"


def test_settings_proxy_url_empty_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "12345:TEST_TOKEN")
    monkeypatch.setenv("PROXY_URL", "   ")
    assert Settings.from_env().proxy_url is None


def test_build_session_uses_proxy_when_set() -> None:
    from aiogram.client.session.aiohttp import AiohttpSession

    from gatecheck_bot.__main__ import build_session, mask_proxy_url

    settings = Settings(
        bot_token="12345:TEST_TOKEN",
        admin_ids=frozenset({1}),
        proxy_url="socks5://user:secret@127.0.0.1:1080",
    )
    session = build_session(settings)
    assert isinstance(session, AiohttpSession)
    # Прокси доходит до сессии, а в логах не светится пароль.
    assert "secret" not in mask_proxy_url(settings.proxy_url)
    assert "127.0.0.1:1080" in mask_proxy_url(settings.proxy_url)


def test_network_error_text_hints_proxy() -> None:
    from gatecheck_bot.__main__ import network_error_text

    with_proxy = Settings(
        bot_token="12345:TEST_TOKEN",
        admin_ids=frozenset({1}),
        proxy_url="socks5://127.0.0.1:1080",
    )
    without_proxy = Settings(bot_token="12345:TEST_TOKEN", admin_ids=frozenset())
    assert "прокси" in network_error_text(with_proxy, TimeoutError())
    assert "PROXY_URL" in network_error_text(without_proxy, TimeoutError())
