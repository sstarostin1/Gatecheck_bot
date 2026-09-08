# Spike-01: проверка внешних источников (M0)

Дата: 2026-09-08. Цель по VISION.md §10 (M0): подтвердить доступность RedisQ, zK API, ESI и
поведение `locationID`. Пока сделана только лёгкая проверка из сети автора (Win11) и из песочницы
ассистента.

## Результаты на сегодня

| Проверка | Среда | Результат | Вывод |
|---|---|---|---|
| ESI `universe/constellations`, `universe/names`, `search` | песочница ассистента | ✅ работает (собран состав Hed и др.) | ESI доступен |
| zKillboard `zkillboard.com/api/` (доки) | песочница ассистента | ✅ работает | zK доступен |
| RedisQ `redisq.zkillboard.com` | песочница ассистента | ❌ DNS не резолвится | ограничение среды, не факт что проблема на стороне zK |
| RedisQ `curl -s https://redisq.zkillboard.com/listen.php?ttw=3` | Win11 автора (cmd) | ⚠️ пустой ответ за ~0с (ожидали `{"package":null}`) | неоднозначно: нужен повтор с UA и замером HTTP-кода |
| gatecamp.watch | обе среды | ❌ DNS/недоступен | OQ-9 остаётся открытым |

## Следующие шаги спайка

1. RedisQ повторить с этикетным User-Agent и выводом кода ответа:
   `curl -v -A "GatecheckBot/0.1 (repo-url)" "https://redisq.zkillboard.com/listen.php?ttw=3"`
   Критерий успеха: HTTP 200 и тело `{"package":null}` либо пакет с killmail.
2. Проверить то же самое с VPS (финальный вердикт по OQ-1, D3 fallback остаётся в силе).
3. zK API: взять свежие киллы Amamake: `https://zkillboard.com/api/solarSystemID/30002537/pastSeconds/3600/`
   и проверить, заполнен ли `zkb.locationID` и соответствует ли он itemID гейта (OQ-2/D4).
4. ESI: собрать граф гейтов региона Heimatar (10000030) в json, оценить размер/время.
