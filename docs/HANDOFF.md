# HANDOFF — промпт передачи проекта в новую сессию ассистента

> Как использовать: в новой сессии Cline (с обнулённым контекстом), открытой на папке
> `d:\soft\samodel\Gatecheck_bot`, первой строкой написать:
> **«Прочитай docs/HANDOFF.md и продолжай работу строго по нему»**.
> В этом файле — всё состояние проекта, соглашения и план.

## 0. Среда и способ работы

- Отвечать пользователю **на русском**.
- Рабочая папка проекта: **`d:\soft\samodel\Gatecheck_bot`** (старый путь с `!!!` пользователь удалил).
- Если терминал Cline не стартует с ошибкой про несуществующий cwd — значит, задача всё ещё открыта
  со старым корнем workspace. Обход: создать файл-заглушку `d:\soft\!!!samodel\Gatecheck_bot\RELOCATED.txt`
  (editor сам создаст папку), все команды давать в виде `cd /d/soft/samodel/Gatecheck_bot && ...`,
  в конце напомнить пользователю: перезагрузить окно VS Code на новый путь и удалить папку-заглушку.
- В bash не использовать `!` в путях (history expansion, «event not found»).
- Файлы — инструментом editor по абсолютным путям; shell-команды — относительными путями из корня проекта.
- После каждой значимой итерации: обновлять журнал версий VISION.md (§12), статусы OQ (§11),
  факты в `docs/research/spike-01.md`; коммитить атомарно (префиксы chore/fix/feat/docs).

## 1. Первым делом в новой сессии

1. Прочитать `docs/VISION.md` (идеология, решения D1–D9, открытые вопросы OQ-1…OQ-12).
2. Прочитать `docs/research/spike-01.md` (результаты и план спайка).
3. `git log --oneline` и `git status` — понять, что уже закоммичено.
4. Прогнать проверки: `.venv/Scripts/python -m pytest -q` и `.venv/Scripts/python -m ruff check .`

## 2. Проект в двух словах

Telegram-бот для сальваг-фармеров EVE Online. Два режима: (A) фоновый мониторинг зоны фарма —
v1 это пресет «констелляция Hed + примыкающие системы» (lowsec, Heimatar) с пуши о всплесках
киллов на воротах (≥3/10 мин, ≥8/час, droppable ISK выше порога — D5); (B) активный мониторинг
маршрута A→B: локальный BFS по графу гейтов, опрос каждые 40–60 с, алерт на каждый новый килл
на воротах по пути, TTL 1 час (D6). Бот публичный (D1), философия — «личный инструмент, доступный
другим» (D9). Стек: Python 3.13 + aiogram 3 + SQLite + asyncio, один процесс (D2). Деплой: слабый
VPS Debian 13, автодеплой GitHub Actions по SSH (D8). Данные: zKillboard API + ESI; парсить
сторонние гейтчек-сайты НЕ нужно (у eve-gatecheck.space нет API).

## 3. Текущий статус (что уже сделано)

- Скелет бота: `gatecheck_bot/` (config.py — Settings из `.env`; handlers.py — `/start`, `/help`,
  `/ping` + эхо на любой текст/медиа; `__main__.py` — long polling), `main.py`, `run.bat`.
- Graceful shutdown: `get_me()` с таймаутом 20 с → внятная ошибка про сеть; `try/finally` закрывает
  сессию (лечит `Unclosed client session` при Ctrl+C).
- Тесты `tests/test_smoke.py` (4 шт), ruff чист, pyproject (ruff line-length 100, pytest).
- Git: ветка `main`, идентичность локальная: Anewkey / Anewkey@users.noreply.github.com (заменить
  при необходимости).
- `.env` у пользователя уже создан и содержит реальный BOT_TOKEN — **не читать, не коммитить**.
- Пользователь ставил aiogram и в глобальный python — не страшно; стандарт запуска: `run.bat`
  или `.venv\Scripts\python main.py`.
- venv создан (`.venv/`, Python 3.13.7, aiogram 3.31.0, python-dotenv, pytest, ruff).
- **v0.2.0 (2026-09-08):** добавлен `PROXY_URL` — Telegram через HTTP/SOCKS5-прокси
  (`AiohttpSession(proxy=...)`; aiohttp-socks 0.12 — обязательная зависимость, aiogram использует
  его для любого прокси). Стартовые логи («Проверяю связь…» до get_me) и внятные ошибки: таймаут
  20 с, мёртвый прокси (семья ProxyError из aiohttp_socks — НЕ наследники OSError, ловится явно),
  401 токен. Пароль прокси маскируется в логе (`mask_proxy_url`). Создан отсутствовавший
  `.env.example`; `run.bat`: `chcp 65001` + `@echo off`. Тесты 8/8, ruff чист. В обе ветки ошибок
  проверены живыми прогонами. **Из песочницы и сети автора api.telegram.org недоступен напрямую**
  (curl timeout) — боту нужен PROXY_URL в `.env`.
- **v0.3.0 (2026-09-09): автономный транспорт.** Новый пакет `gatecheck_bot/transport/`
  (proxy_types — парсинг/фильтр, sources — добыча из открытых списков, checker — живая проба
  api.telegram.org через прокси + TCP-предпроба, xray — vless→локальный socks5, pool — кэш/ротация).
  Стратегия: статичный PROXY_URL → прямой доступ → пул (добыча → проверка → кэш → вотчдог →
  повторная добыча). MTProto-прокси отфильтровываются (Bot API несовместим). CLI-диагностика:
  `python -m gatecheck_bot.proxycheck`. Ruff 0.16: BLE001/S110 в ignore (осознанные широкие catch
  на сетевых границах). Тесты 18/18 (`tests/test_transport.py`). **Проверено живьём:** из сети
  с блокировкой Telegram бот добыл пул и запустился через socks5 из него (@Eve_gatechecker_bot).
- **v0.4.0 (2026-09-09): модуль M1 «граф+маршрут», M0 закрыт.** `scripts/fetch_static.py`
  (ESI → `data/graph.json` + `data/gates.json`; Heimatar — 83 системы / 238 гейта / 220 рёбер,
  конкурентность 8, gzip, UA). Модуль `gatecheck_bot/routing.py` (резолв имён + BFS) и команда
  `/route A B` в aiogram-хендлерах (граф грузится лениво; собрать можно на живом боту).
  **OQ-2 закрыт**: zK `locationID` = itemID гейтов (проверено на 194 киллах Amamake) → D4 без
  геометрии, OQ-3 снят. Тесты 28/28 (`tests/test_routing.py`), ruff чист. Живые маршруты:
  Amamake→Siseide 1 прыжок; Amamake→Rens 3 (Osoggur, Abudban).

## 4. Открытые проблемы на момент передачи

1. **Ждёт проверки пользователем (после v0.3.0):** запустить `run.bat` — при блокировке Telegram
   бот сам поднимет прокси-пул (первый старт ~1–2 мин: добыча + проверка; далее мгновенно из
   кэша), дождаться «запущен как @…» и проверить `/start` + эхо. Опционально: свой прокси в
   `PROXY_URL` (приоритет), свои источники в `PROXY_SOURCES`. Идея автора про открытые
   агрегаторы прокси — OQ-13 (система источников плагинная).
2. **RedisQ — закрыто (2026-09-08):** в доках zK навигация показывает **«LIVE UPDATES DISABLED»**,
   `redisq.zkillboard.com` не резолвится из двух независимых сетей. Сам zK API работает: HTTP 200
   с живыми киллами Amamake (`solarSystemID/30002537/pastSeconds/3600/`, ~1.3 с), в билдере есть
   модификатор `locationID`. **D3 финализирован: polling zK API.** Детали — spike-01.md.
3. Терминал/пути — см. §0 (перезагрузка окна VS Code на новый путь).

## 5. Дорожная карта (по приоритету)

- **M0 (спайк): ЗАКРЫТ 2026-09-09** — OQ-2 подтверждён на живых данных: `zkb.locationID` =
  itemID гейтов → D4 без геометрии (OQ-3 снят). RedisQ мёртв (LIVE UPDATES DISABLED) →
  D3 = polling zK API, сам API отвечает 200 с живыми киллами. Детали — spike-01.md.
- **M1: ЧАСТИЧНО ГОТОВО (v0.4.0)** — `scripts/fetch_static.py` (граф+гейты Heimatar) и
  `/route A B` (BFS) работают; **осталось:** systemd-юнит для Debian 13 и GitHub Actions
  (ruff+pytest, деплой-джоба по SSH, D8).
- **M2:** фоновый мониторинг зоны: poller zK API (зона ~5–8 req/мин — в рамках этикета), dedup по
  killID, `kill_cache` в SQLite, пресет «Hed+соседи», алерты D5 (всплеск ≥3/10 мин; ≥8/час;
  droppable ISK — OQ-11: поля zkb vs `quantity_dropped` × цены ESI/Fuzzwork), cooldown.
  Фундамент готов: киллы на воротах = `zkb.locationID` ∈ itemID гейтов системы (OQ-2 закрыт).
- **M3:** активный режим маршрута: TTL 1 ч (D6), опрос 40–60 с, алерт ≥1 нового килла на воротах,
  группировка ship+pod (OQ-4), `/route stop`.
- **M4:** fair-use лимиты на чат (OQ-8), `/settings`, `/ping` со статистикой, README с деплоем,
  публикация на GitHub.

## 6. Проверенные факты (не переисследовать)

- ESI base: `https://esi.evetech.net/latest`. **Hed — это Heimatar (10000030), НЕ Delve!**
  Констелляция Hed = 20000372: Amamake 30002537, Vard 30002538, Siseide 30002539, Lantorn 30002540,
  Dal 30002541, Auga 30002542. Delve = 10000060 (к нам не относится).
- zK API: `GET https://zkillboard.com/api/{modifiers}/`; ≤200 киллов/страницу, `page/1..100`;
  модификаторы: `solarSystemID/{id}` (алиас `systemID`), `constellationID`, `regionID`,
  **`locationID/{id}`** (resolved location — кандидат «килл на воротах»), `killID/{id}`,
  `pastSeconds/{sec}` до 604800 с шагом 1 час, `year/{YYYY}/month/{M}`; ОТКЛЮЧЕНЫ: `startTime/endTime`,
  `orderDirection`, `limit`, `zkbOnly`, `no-attackers`, `no-items`, `asc/desc`, `json/xml`.
  Этикет: осмысленный User-Agent с URL проекта, gzip, кэшировать, паузы, trailing slash.
- eve-gatecheck.space (EVE Gatecamp Check): публичного API НЕТ (`/api` → 404), премиум монетизирован.
  Референс порогов: 0 / ≤2 / ≥3 киллов у гейтов за час + маркеры smartbomb-киллов и hic/dictor.
- `gatecamp.watch` — не проверен (DNS не резолвился из обеих сетей); OQ-9.
- Цены для droppable ISK (OQ-11): ESI `/markets/prices/` или Fuzzwork; уточнить поля zkb
  (`totalValue`, `droppedValue`?) в спайке.

## 7. Соглашения

- Тексты интерфейса и докстринги — на русском (как в скелете); коммит-месседжи — на английском.
- `ruff` и `pytest` обязаны проходить до коммита.
- Секреты — только в `.env` (в `.gitignore`); в репо — `.env.example` без секретов.

