# Gatecheck Bot

Телеграм-бот мониторинга киллов на гейтах EVE Online: пуш-уведомления о всплесках активности
в зоне фарма и усиленный мониторинг маршрутов. Идеология и план — в [docs/VISION.md](docs/VISION.md).

**Текущий статус (v0.7.0):** задеплоен на VPS (Debian 13, systemd — см. [deploy/README.md](deploy/README.md)),
работает при блокировках (автономный прокси-пул), состояние переживает рестарт (SQLite), маршруты
по всему Новому Эдену со слежением гейт-кампов (`/route Amamake Jita`), мониторинг зоны фарма
(`/zone on`, алерты D5) с настраиваемыми порогами (`/settings`). Публичный репозиторий:
https://github.com/sstarostin1/Gatecheck_bot — план в [docs/VISION.md](docs/VISION.md).

## Быстрый старт (Windows)

Проще всего — из cmd в папке проекта:

```bat
run.bat
```

Скрипт сам создаст `.venv`, поставит зависимости и при первом запуске создаст `.env` из шаблона
(останется вписать `BOT_TOKEN` и запустить снова). Затем один раз собери граф гейтов (для `/route`):

```bat
.venv\Scripts\python scripts\fetch_static.py
```

Вручную то же самое:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # и вписать BOT_TOKEN от @BotFather
.venv\Scripts\python scripts\fetch_static.py
.venv\Scripts\python main.py
```

⚠️ Запускать нужно интерпретатором из `.venv` (или через `run.bat`), а не глобальным `python` —
иначе получите `ModuleNotFoundError: No module named 'aiogram'`.

Затем в Telegram: `/start` → приветствие; `/zone on` → мониторинг зоны фарма «Hed + соседи»
(алерты всплесков на гейтах); `/route Amamake Jita` → маршрут (15 прыжков) + слежение гейт-кампов
(TTL 1 ч); `/route status`, `/zone status` — статистика; `/settings` — пороги алертов;
`/route stop`, `/zone off` — выключить; любой текст → эхо. Подписки переживают рестарт бота
(SQLite).

## Если что-то не работает

- `ModuleNotFoundError: No module named 'aiogram'` — запущен глобальный python без зависимостей.
  Используйте `.venv\Scripts\python main.py` или `run.bat`.
- **Telegram заблокирован в вашей сети?** Бот справится сам (v0.3.0): прямой доступ отпадает →
  включается автономный прокси-пул — кандидаты добываются из открытых списков (GitHub raw,
  доступен даже при блокировке Telegram), проверяются реальным запросом к api.telegram.org
  через сам прокси, рабочие кэшируются в `data/proxy_cache.json` и ротируются вотчдогом;
  пул исчерпан — добыча запускается заново. Ничего настраивать не нужно. Дополнительно можно
  задать свой прокси `PROXY_URL` в `.env` (HTTP/SOCKS5 — например, локальный порт
  v2rayN/Clash/Proxifier) — он будет использован первым. Диагностика пула без запуска бота:
  `.venv\Scripts\python -m gatecheck_bot.proxycheck`.
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
| `PROXY_URL` | нет | статичный прокси (HTTP/SOCKS5) — используется первым, до автопула |
| `PROXY_AUTOPOOL` | нет | `1` (по умолчанию) — автономный пул: добыча/проверка/ротация прокси |
| `PROXY_MANUAL` | нет | свои прокси через `\|`: `socks5://…`, `http://…`, `vless://…` (приоритетнее списков) |
| `PROXY_SOURCES` | нет | свои URL-списки через запятую (по умолчанию — проверенные GitHub-списки) |
| `PROXY_CACHE` | нет | файл кэша рабочих прокси (по умолчанию `data/proxy_cache.json`) |

## Разработка и тесты

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
```

Структура: `main.py` (запуск) · `gatecheck_bot/` (config, handlers, __main__) · `tests/` · `docs/`.
