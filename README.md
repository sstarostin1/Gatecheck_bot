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
- Бот «висит» на строке `Проверяю связь с Telegram...`, затем падает с
  `Не удалось связаться с api.telegram.org за 20 с` — Telegram недоступен из вашей сети
  напрямую. Впишите в `.env` рабочий прокси: `PROXY_URL=socks5://127.0.0.1:1080`
  (или `http://...`) и запустите снова. Поддерживаются HTTP- и SOCKS5-прокси
  (например, локальный порт проксификатора: v2rayN, Clash, Proxifier).
- `Telegram отклонил токен (401 Unauthorized)` — неверный `BOT_TOKEN` в `.env`
  (токен выдаёт @BotFather).
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
| `PROXY_URL` | нет | прокси для api.telegram.org: `socks5://127.0.0.1:1080` или `http://user:pass@host:port`. Нужен, если Telegram недоступен напрямую из сети |

## Разработка и тесты

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
```

Структура: `main.py` (запуск) · `gatecheck_bot/` (config, handlers, __main__) · `tests/` · `docs/`.
