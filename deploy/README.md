# Деплой на VPS (D8) — скрипт деплоя, ручной цикл, автодеплой GH Actions (позже)

Сервер: слабый VPS (1 vCPU / 1 GB RAM / 10 GB NVMe). Реквизиты — в gitignored `data/vps.txt`
(НЕ коммитить: репо публичное). Полный чек-лист первого подключения — там же.

## Деплой одной командой — `scripts/deploy.py` (основной способ с v0.11.1)

Из корня проекта (cmd → `deploy.bat`, либо git-bash/WSL → `python scripts/deploy.py`):

```bash
python scripts/deploy.py --dry-run   # прогнать все проверки, прод НЕ трогать
python scripts/deploy.py             # боевая выкатка + верификация
deploy.bat                           # то же из cmd (обёртка над скриптом)
```

Что делает скрипт (любой провал → ненулевой код возврата и подсказка об откате):

1. **Preflight:** ветка `main`, чистое рабочее дерево, доступность `ssh` и сервера (только чтение).
2. **Локальные проверки ДО выкатки:** `ruff check .` + `pytest -q` (обязательны, кроме `--no-tests`).
3. **Публикация:** `git push origin main`, если есть незапушенные коммиты, + сверка SHA.
4. **Обновление кода** на сервере от юзера `gatecheck`: `git fetch --prune` + `git reset --hard origin/main`
   (иначе `git` под root ловит `dubious ownership`), сверка HEAD с локальным SHA.
5. **Зависимости:** `pip install -r requirements.txt` — только если файл изменился между коммитами.
6. **Рестарт:** `systemctl restart gatecheck` + пауза (по умолчанию 6 с, `--wait`).
7. **Верификация (обязательная):** сервис `active`; в журнале есть строка
   `Gatecheck Bot vX запущен как @…` и версия **совпала** с `gatecheck_bot/__init__.py`;
   память в пределах `MemoryMax`; строки `| ERROR`/`Traceback` после рестарта выводятся в отчёт
   (провал при `--strict-errors`).
8. **Откат:** при провале печатаются команды отката на прежний SHA; с `--rollback-on-fail`
   откат выполняется автоматически.

Полезные флаги: `--dry-run`, `--no-push`, `--no-tests`, `--allow-dirty`, `--force-restart`
(рестарт, даже если код не менялся), `--strict-errors`, `--rollback-on-fail`, `--wait N`.
Параметры подключения — через env (по умолчанию прод проекта):
`VPS_ALIAS=vps` (алиас из `~/.ssh/config`), либо `VPS_HOST`/`VPS_USER`;
`VPS_PATH=/opt/gatecheck`, `VPS_SERVICE=gatecheck`, `VPS_RUN_AS=gatecheck`.
Секретов в скрипте и логах нет — вход только по SSH-ключу.

Пример вывода успешного прогона: preflight → ruff/pytest → push → reset → pip (если нужно) →
restart → «в журнале версия vX совпала с локальной» → «Деплой vX завершён и проверен».

## Ручной цикл (чем занимался скрипт до v0.11.1)

1. `git push` (локально) → 2. `ssh vps` → 3. `runuser -u gatecheck -- git -C /opt/gatecheck pull` →
4. при изменениях `requirements.txt`: `runuser -u gatecheck -- /opt/gatecheck/.venv/bin/pip install -r requirements.txt` →
5. `systemctl restart gatecheck` → 6. `journalctl -u gatecheck -n 30 --no-pager` (строка версии) →
7. проверка `/ping` в Telegram.

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

## Грабли VPS

- **Не делайте git-операции на сервере от `root`** (`git pull`, `reset`): новые файлы и объекты
  остаются root-овыми, и потом `gatecheck` не может ни обновить репозиторий
  (`error: insufficient permission for adding an object to repository database .git/objects`),
  ни перезаписать файлы при `git reset`. Диагностика:
  `ssh vps "find /opt/gatecheck -user root | wc -l"` (должно быть 0).
  Лечение (один раз, от root): `ssh vps "chown -R gatecheck:gatecheck /opt/gatecheck"`.
  Именно этот случай случился 19.09.2026 (деплой v0.10.0 от root → 135 root-файлов) и был
  выявлен скриптом деплоя; исправлено 01.10.2026.
- Скрипт `scripts/deploy.py` сам выполняет git от юзера `gatecheck` (через `runuser`) и при
  ошибке прав печатает готовую команду лечения.

## Тонкости слабого VPS

- лимит памяти в юните (MemoryMax=400M) — при OOM systemd перезапустит; состояние переживает
  рестарт благодаря SQLite (подписки/маршруты/kill-кэш);
- граф Нового Эдена (~3 МБ json) грузится лениво и один раз;
- zK/ESI ходят напрямую (из доступны), Telegram — через PROXY_URL/автопул при необходимости.
