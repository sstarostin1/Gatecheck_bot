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

## «Автообновление статики» — что это значит

Статика = `data/graph.json` + `data/gates.json` (граф гейтов Нового Эдена из ESI).
Она почти не меняется (CCP меняет карту раз в год), но ВРЕМЯ ОТ ВРЕМЕНИ обновлять нужно:
появились новые системы/гейты, переименования. Сейчас на VPS она обновляется вручную:

```bash
ssh vps "cd /opt/gatecheck && runuser -u gatecheck -- .venv/bin/python scripts/fetch_static.py && systemctl restart gatecheck"
```

(пересборка ~4 мин: ~18 тыс. запросов к ESI; рестарт нужен, чтобы бот перечитал файлы).
«Автообновление» = systemd-таймер (например, раз в сутки ночью), который делает то же самое
без человека. Добавить на VPS:

```bash
/etc/systemd/system/gatecheck-static.service   # Type=oneshot — одна пересборка
/etc/systemd/system/gatecheck-static.timer     # OnCalendar=*-*-* 04:00:00 UTC
```

Приоритет низкий: без этого бот работает на текущей статике неограниченно долго.

## «Автодеплой GH Actions» — как работает

Сейчас релиз = 3 команды руками (локально): `git push` → `ssh vps "git pull && restart"`.
Автодеплой перекладывает это на GitHub: после каждого пуша в main GitHub сам запускает
воркфлоу (в `.github/workflows/`), который: 1) гоняет ruff+pytest (уже есть), 2) подключается
к VPS по SSH и выполняет `git pull && systemctl restart gatecheck`. Чтобы GitHub мог
подключиться к серверу, нужно ОДИН раз настроить:

1. Сгенерировать отдельный ключ для деплоя: `ssh-keygen -t ed25519 -f deploy_key -N ""`,
   публичную половину добавить на VPS в `/home/gatecheck/.ssh/authorized_keys`
   (деплой должен работать от юзера gatecheck, не root).
2. В GitHub: Settings → Secrets and variables → Actions → добавить три секрета:
   `VPS_HOST` (147.45.124.83), `VPS_USER` (gatecheck), `VPS_SSH_KEY` (содержимое deploy_key).
3. Добавить job в ci.yml: шаги ssh-agent + rsync/pull + рестарт.

После этого релиз = `git push` — тесты и выкатка происходят сами, в течение ~1 мин.

## Тонкости слабого VPS

- лимит памяти в юните (MemoryMax=400M) — при OOM systemd перезапустит; состояние переживает
  рестарт благодаря SQLite (подписки/маршруты/kill-кэш);
- граф Нового Эдена (~3 МБ json) грузится лениво и один раз;
- zK/ESI ходят напрямую (из доступны), Telegram — через PROXY_URL/автопул при необходимости.
