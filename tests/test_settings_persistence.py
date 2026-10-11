"""Регрессия B2 (docs/BACKLOG.md): пороги применяются из Storage.

Корневая причина была в том, что build_router() вызывался без storage, поэтому
все /settings_* отвечали «Хранилище недоступно». Эти тесты фиксируют, что
проброс storage в роутер реально доводит пользовательские пороги до Storage.
"""

from aiogram.filters import CommandObject

from gatecheck_bot.config import Settings
from gatecheck_bot.handlers import build_router
from gatecheck_bot.storage import Storage


class FakeMessage:
    """Минимальный мок aiogram Message: chat.id + await-answer()."""

    def __init__(self, chat_id: int) -> None:
        self.chat = type("Chat", (), {"id": chat_id})()
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)


def _get_handler(router, name: str):
    """Найти замыкание-хендлер по имени функции в message-наблюдателе роутера."""
    for h in router.message.handlers:
        if h.callback.__name__ == name:
            return h.callback
    raise AssertionError(f"хендлер {name} не зарегистрирован в роутере")


def _run(coro) -> None:
    import asyncio

    asyncio.run(coro)


def test_settings_hour_saved_via_router_with_storage(tmp_path) -> None:
    """storage проброшен в build_router → /settings_hour 10 пишется в Storage."""
    storage = Storage(tmp_path / "b2.sqlite3")
    settings = Settings(bot_token="12345:TEST_TOKEN", admin_ids=frozenset({1}))
    router = build_router(settings, storage=storage)

    handler = _get_handler(router, "cmd_settings_hour")
    msg = FakeMessage(chat_id=7)
    _run(handler(msg, CommandObject(command="settings_hour", args="10")))

    assert storage.get_settings(7)["hour"] == 10.0
    assert any("hour = 10" in a for a in msg.answers)
    storage.close()


def test_settings_view_shows_saved_value(tmp_path) -> None:
    """/settings (без аргументов) показывает сохранённое значение, не дефолт."""
    storage = Storage(tmp_path / "b2.sqlite3")
    storage.set_settings(7, {"hour": 12.0})
    settings = Settings(bot_token="12345:TEST_TOKEN", admin_ids=frozenset({1}))
    router = build_router(settings, storage=storage)

    handler = _get_handler(router, "cmd_settings")
    msg = FakeMessage(chat_id=7)
    _run(handler(msg, CommandObject(command="settings")))

    text = "\n".join(msg.answers)
    assert "hour: 12 киллов" in text
    storage.close()


def test_settings_without_storage_reports_unavailable() -> None:
    """Страж-тест: без storage команда честно отвечает «Хранилище недоступно»."""
    settings = Settings(bot_token="12345:TEST_TOKEN", admin_ids=frozenset({1}))
    router = build_router(settings, storage=None)

    handler = _get_handler(router, "cmd_settings_hour")
    msg = FakeMessage(chat_id=7)
    _run(handler(msg, CommandObject(command="settings_hour", args="10")))

    assert any("Хранилище недоступно" in a for a in msg.answers)
