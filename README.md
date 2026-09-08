# Gatecheck Bot

Телеграм-бот мониторинга киллов на гейтах EVE Online: пуш-уведомления о всплесках активности
в зоне фарма и усиленный мониторинг маршрутов. Идеология и план — в [docs/VISION.md](docs/VISION.md).

**Текущий статус: скелет (v0.1.0).** Бот запускается, получает сообщения и отвечает:
`/start`, `/help`, `/ping` и эхо на любой текст/медиа. Мониторинг — следующие итерации.

## Быстрый старт (Windows)

Проще всего — из cmd в папке проекта:

```bat
run.bat
```

Скрипт сам создаст `.venv`, поставит зависимости и при первом запуске создаст `.env` из шаблона
(останется вписать `BOT_TOKEN` и запустить снова). Вручную то же самое:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # и вписать BOT_TOKEN от @BotFather
.venv\Scripts\python main.py
```

⚠️ Запускать нужно интерпретатором из `.venv` (или через `run.bat`), а не глобальным `python` —
иначе получите `ModuleNotFoundError: No module named 'aiogram'`.

Затем в Telegram: `/start` → бот отвечает; любой текст → эхо. Это подтверждает приём/отправку.

## Если что-то не работает

- `ModuleNotFoundError: No module named 'aiogram'` — запущен глобальный python без зависимостей.
  Используйте `.venv\Scripts\python main.py` или `run.bat`.
- `Не удалось связаться с api.telegram.org за 20 с` — сеть не достаёт до Telegram: проверьте
  VPN/прокси (поддержка прокси в конфиге — в плане, VISION.md §9).
- Бот «запущен», но молчит: убедитесь, что пишете именно этому боту и сначала отправили `/start`;
  убедитесь, что не запущен второй экземпляр (getUpdates выдаёт 409 Conflict — он был бы в логе).


## Linux (VPS, Debian 13)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env   # BOT_TOKEN
.venv/bin/python main.py
```

systemd-юнит и автодеплой (GitHub Actions по SSH) — на этапе M1, см. VISION.md §10.

## Конфигурация

| Переменная | Обязательна | Описание |
|---|---|---|
| `BOT_TOKEN` | да | токен от [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | нет | TG ID админов через запятую (свой ID — у @userinfobot) |

## Разработка и тесты

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
```

Структура: `main.py` (запуск) · `gatecheck_bot/` (config, handlers, __main__) · `tests/` · `docs/`.
