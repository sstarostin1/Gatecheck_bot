# Деплой на VPS (D8) — подготовка; автодеплой GH Actions будет позже

Сервер: слабый VPS (1 vCPU / 1 GB RAM / 10 GB NVMe). Реквизиты — в gitignored `data/vps.txt`
(НЕ коммитить: репо публичное). Полный чек-лист первого подключения — там же.

## Ручной деплой (пока без GH Actions)

1. Залить код: `rsync -av --exclude .venv --exclude data/ --exclude .git ./ root@IP:/opt/gatecheck/`
   (или git clone, когда репо опубликовано).
2. На сервере: `cd /opt/gatecheck && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `.env` с BOT_TOKEN/ADMIN_IDS (вручную, не из репо) + `.venv/bin/python scripts/fetch_static.py`
4. `cp deploy/gatecheck.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now gatecheck`
5. Логи: `journalctl -u gatecheck -f`. Проверка: `/ping` в Telegram.

## Автодеплой (позже, D8)

GitHub Actions: пуш в main → ruff+pytest → rsync по SSH → `systemctl restart gatecheck`.
Секреты (SSH-ключ, хост) — в GitHub Secrets. Джобу добавить после публикации репо.

## Тонкости слабого VPS

- лимит памяти в юните (MemoryMax=400M) — при OOM systemd перезапустит; состояние переживает
  рестарт благодаря SQLite (подписки/маршруты/kill-кэш);
- граф Нового Эдена (~3 МБ json) грузится лениво и один раз;
- zK/ESI ходят напрямую (из доступны), Telegram — через PROXY_URL/автопул при необходимости.
