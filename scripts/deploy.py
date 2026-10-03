"""Деплой Gatecheck Bot на прод одной командой — с обязательными проверками.

Полный цикл (любой провал → ненулевой код возврата и подсказка об откате):

  1) preflight — ветка main, чистое дерево, доступность ssh и сервера (только чтение);
  2) локальные проверки — ruff + pytest ДО выкатки (обязательны, кроме --no-tests);
  3) git push origin main (если есть незапушенные коммиты) + сверка SHA;
  4) на сервере — git fetch --prune + git reset --hard origin/main от юзера gatecheck;
  5) pip install -r requirements.txt, если файл изменился между прежним и новым SHA;
  6) systemctl restart gatecheck;
  7) верификация — сервис active, в журнале «Gatecheck Bot vX запущен как @…» с версией из
     gatecheck_bot/__init__.py, память в пределах MemoryMax, ошибки после рестарта — в отчёт;
  8) при провале — команды отката (или авто-откат с --rollback-on-fail).

Запуск из корня проекта (cmd/git-bash/WSL):

    python scripts/deploy.py --dry-run   # только проверки, прод не трогаем
    deploy.bat                           # то же из cmd (обёртка над этим скриптом)

Параметры (env; значения по умолчанию — прод проекта):
    VPS_ALIAS=vps            # алиас из ~/.ssh/config, если не задан VPS_HOST
    VPS_HOST=147.45.124.83   VPS_USER=root
    VPS_PATH=/opt/gatecheck  VPS_SERVICE=gatecheck  VPS_RUN_AS=gatecheck

Секретов в скрипте нет: вход только по SSH-ключу (реквизиты — gitignored data/vps.txt).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "gatecheck_bot" / "__init__.py"
BRANCH = "main"
STARTUP_MARK = "Gatecheck Bot v"
OK, FAIL, INFO = "✓", "✗", "•"

DEFAULTS = {
    "VPS_ALIAS": "vps",
    "VPS_HOST": "",
    "VPS_USER": "root",
    "VPS_PATH": "/opt/gatecheck",
    "VPS_SERVICE": "gatecheck",
    "VPS_RUN_AS": "gatecheck",
}


def read_version(version_file: Path = VERSION_FILE) -> str:
    """Версия бота из gatecheck_bot/__init__.py (без импорта пакета)."""
    match = re.search(r'__version__\s*=\s*"([^"]+)"', version_file.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"{FAIL} Не нашёл __version__ в {version_file}")
    return match.group(1)


def parse_journal_version(journal: str) -> str | None:
    """Версия из строк старта «Gatecheck Bot vX.Y.Z запущен как @bot» (последняя в логе)."""
    found = re.findall(rf"{re.escape(STARTUP_MARK)}([\w.]+)", journal)
    return found[-1] if found else None


def parse_prop(props: str, name: str) -> int:
    """Числовое поле из вывода `systemctl show -p …` (0, если поля нет)."""
    match = re.search(rf"^{name}=(\d+)$", props, re.MULTILINE)
    return int(match.group(1)) if match else 0


def ssh_target(env: dict[str, str] | None = None) -> str:
    """Цель для ssh: user@host из env, иначе алиас из ~/.ssh/config (по умолчанию vps)."""
    env = env if env is not None else dict(os.environ)
    host = env.get("VPS_HOST", "").strip()
    if host:
        user = env.get("VPS_USER", DEFAULTS["VPS_USER"]).strip() or DEFAULTS["VPS_USER"]
        return f"{user}@{host}"
    return env.get("VPS_ALIAS", DEFAULTS["VPS_ALIAS"]).strip() or DEFAULTS["VPS_ALIAS"]


class Deployer:
    """Прогон одного деплоя: состояние (SHA, версия) + печать шагов."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        env = dict(os.environ)
        self.target = ssh_target(env)
        self.path = env.get("VPS_PATH", DEFAULTS["VPS_PATH"]).strip() or DEFAULTS["VPS_PATH"]
        self.service = env.get("VPS_SERVICE", DEFAULTS["VPS_SERVICE"]).strip()
        self.run_as = env.get("VPS_RUN_AS", DEFAULTS["VPS_RUN_AS"]).strip()
        self.version = read_version()
        self.ssh_user = ""  # заполняется в preflight (нужно понять, нужен ли runuser)
        self.use_runuser = False
        self.old_sha = ""
        self.new_sha = ""
        self.journal_errors: list[str] = []

    # --- вывод -------------------------------------------------------------

    def step(self, text: str) -> None:
        print(f"\n{INFO} {text}", flush=True)

    def ok(self, text: str) -> None:
        print(f"  {OK} {text}", flush=True)

    def note(self, text: str) -> None:
        print(f"  {INFO} {text}", flush=True)

    def die(self, text: str) -> None:
        raise SystemExit(f"\n{FAIL} {text}")

    # --- обёртки над процессами --------------------------------------------

    def local(self, args: list[str], *, check: bool = True) -> str:
        result = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if check and result.returncode != 0:
            self.die(f"Команда {' '.join(args)} упала (код {result.returncode}):\n{output}")
        return output

    def ssh(self, command: str, *, check: bool = True, timeout: int = 120) -> str:
        """ssh с ретраями: соединение до слабого VPS иногда срывается (код 255)."""
        attempts = 3
        result: subprocess.CompletedProcess[str] | None = None
        output = ""
        for attempt in range(1, attempts + 1):
            result = subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                    "-o", "ServerAliveInterval=15", self.target, command,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout + 60, check=False,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            if result.returncode != 255:
                break
            if attempt < attempts:
                self.note(
                    f"ssh: соединение сорвалось ({self.last_line(output)}), "
                    f"повтор {attempt + 1}/{attempts}…"
                )
                time.sleep(3)
        if result is None or result.returncode == 255:
            self.die(
                f"ssh {self.target}: не удалось соединиться трижды (код 255) — "
                f"{self.last_line(output)}\n"
                "Проверь сеть/VPN и повтори запуск: скрипт идемпотентен, повтор безопасен."
            )
        if check and result.returncode != 0:
            self.die(f"ssh {self.target}: команда упала (код {result.returncode}):\n{output}")
        return output

    @staticmethod
    def last_line(text: str) -> str:
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1][:120] if lines else "без вывода"

    def remote_git(self, args: str, *, check: bool = True) -> str:
        """git на сервере от владельца репозитория (у root иначе dubious ownership)."""
        prefix = f"runuser -u {self.run_as} -- " if self.use_runuser else ""
        return self.ssh(f"{prefix}git -C {self.path} {args}", check=check)

    @property
    def dry_run(self) -> bool:
        return bool(self.args.dry_run)

    def check_local_repo(self) -> None:
        self.step("Preflight: локальный репозиторий")
        branch = self.local(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if branch != BRANCH:
            self.die(f"Текущая ветка «{branch}», а деплой идёт только из «{BRANCH}».")
        self.ok(f"ветка {branch}")
        dirty = self.local(["git", "status", "--porcelain"])
        if dirty and not self.args.allow_dirty:
            tail = "\n".join(f"    {line}" for line in dirty.splitlines()[:10])
            self.die(
                "В рабочем дереве есть незакоммиченные изменения — сначала закоммить их "
                f"(или запусти с --allow-dirty, если это осознанно):\n{tail}"
            )
        self.ok("рабочее дерево чистое" if not dirty else "есть локальные правки (--allow-dirty)")

    def check_server(self) -> None:
        self.step(f"Preflight: сервер {self.target}")
        if shutil.which("ssh") is None:
            self.die("Не нашёл ssh в PATH — нужен OpenSSH-клиент.")
        resolved = self.local(["ssh", "-G", self.target], check=False)
        user_match = re.search(r"^user\s+(\S+)$", resolved, re.MULTILINE)
        self.ssh_user = user_match.group(1) if user_match else ""
        self.use_runuser = bool(self.run_as) and self.ssh_user == "root"
        self.ssh("true")
        self.ok(f"ssh работает (вход как {self.ssh_user or '?'})")
        probe = self.ssh(f"test -d {self.path} && echo yes", check=False)
        if not probe.endswith("yes"):
            self.die(f"На сервере нет каталога {self.path} (см. deploy/README.md, чек-лист VPS).")
        self.ok(f"репозиторий {self.path} на месте")
        active = self.ssh(f"systemctl is-active {self.service}", check=False)
        self.ok(f"сервис {self.service}: {active or 'нет ответа'}")
        self.old_sha = self.remote_git("rev-parse HEAD").splitlines()[-1]
        self.note(f"текущий коммит на проде: {self.old_sha[:7]}")

    def run_local_checks(self) -> None:
        if self.args.no_tests:
            self.note("локальные проверки пропущены (--no-tests)")
            return
        self.step("Локальные проверки: ruff + pytest")
        self.local([sys.executable, "-m", "ruff", "check", "."])
        self.ok("ruff чист")
        output = self.local([sys.executable, "-m", "pytest", "-q"])
        self.ok(f"pytest: {output.splitlines()[-1] if output else 'ок'}")

    def push(self) -> None:
        self.step(f"Публикация в origin/{BRANCH}")
        local_sha = self.local(["git", "rev-parse", "HEAD"])
        self.local(["git", "fetch", "origin"])
        remote_sha = self.local(["git", "rev-parse", f"origin/{BRANCH}"], check=False)
        if local_sha == remote_sha:
            self.ok(f"origin/{BRANCH} уже на {local_sha[:7]} — пушить нечего")
        elif self.dry_run:
            self.note(f"DRY-RUN: push не выполняю, было бы {remote_sha[:7]} → {local_sha[:7]}")
            self.new_sha = local_sha
            return
        elif self.args.no_push:
            self.note(f"push пропущен: локально {local_sha[:7]}, в origin {remote_sha[:7]}")
        else:
            self.local(["git", "push", "origin", BRANCH])
            remote_sha = self.local(["git", "rev-parse", f"origin/{BRANCH}"])
            self.ok(f"запушено: {remote_sha[:7]}")
        if local_sha != remote_sha:
            self.die("origin/main и локальный HEAD разошлись — деплой отменён.")
        self.new_sha = remote_sha

    def check_remote_lag(self) -> bool:
        """False — на проде уже этот коммит и рестарт не запрошен: выкатывать нечего."""
        if self.old_sha == self.new_sha and not self.args.force_restart:
            self.ok(f"прод уже на {self.new_sha[:7]} — выкатывать нечего (--force-restart)")
            return False
        return True


    def update_code(self) -> None:
        self.step(f"Обновление кода на сервере до {self.new_sha[:7]}")
        fetched = self.remote_git("fetch --prune origin", check=False)
        if "insufficient permission" in fetched or "unable to create file" in fetched:
            self.hint_permissions(fetched)
        head = self.remote_git("rev-parse origin/main").splitlines()[-1]
        if head != self.new_sha:
            self.die(f"Сервер видит origin/main = {head[:7]}, ожидался {self.new_sha[:7]}.")
        reset = self.remote_git("reset --hard origin/main", check=False)
        if "insufficient permission" in reset or "unable to create file" in reset:
            self.hint_permissions(reset)
        current = self.remote_git("rev-parse HEAD").splitlines()[-1]
        if current != self.new_sha:
            self.die(f"После reset HEAD = {current[:7]}, ожидался {self.new_sha[:7]}.")
        self.ok(f"HEAD = {current[:7]}")
        changed = self.remote_git(f"diff --name-only {self.old_sha} {current}", check=False)
        self.update_deps(changed.splitlines())

    def hint_permissions(self, output: str) -> None:
        """Классические грабли VPS: git-операцию делали от root → файлы стали root-овыми."""
        self.die(
            "На сервере не хватает прав на файлы репозитория (обычно после git-операций от root):\n"
            f"{output}\n"
            "Лечится один раз (от root):\n"
            f"    ssh {self.target} \"chown -R {self.run_as}:{self.run_as} {self.path}\"\n"
            "подробнее — deploy/README.md, раздел «Грабли VPS»."
        )

    def update_deps(self, changed: list[str]) -> None:
        venv_python = f"{self.path}/.venv/bin/python"
        probe = self.ssh(f"test -x {venv_python} && echo yes", check=False)
        if not probe.endswith("yes"):
            self.step("В .venv нет интерпретатора — создаю окружение")
            self.ssh(f"cd {self.path} && {self.runuser_prefix()}python3 -m venv .venv")
            self.ok("окружение создано")
        if "requirements.txt" not in changed:
            self.ok("requirements.txt не менялся — pip не нужен")
            return
        self.step("Установка зависимостей (requirements.txt изменился)")
        self.ssh(
            f"cd {self.path} && {self.runuser_prefix()}.venv/bin/pip install -q -r requirements.txt"
        )
        self.ok("зависимости установлены")

    def restart(self) -> None:
        self.step(f"Рестарт сервиса {self.service}")
        self.ssh(f"systemctl restart {self.service}")
        time.sleep(self.args.wait)
        self.ok(f"пауза {self.args.wait:g} с после рестарта")

    def verify(self) -> None:
        self.step("Верификация выкатки")
        active = self.ssh(f"systemctl is-active {self.service}", check=False)
        if active != "active":
            self.report_failure(f"сервис {self.service} в состоянии «{active}»")
        self.ok("сервис active")
        props = self.ssh(
            f"systemctl show -p MemoryCurrent -p MemoryMax -p NRestarts {self.service}", check=False
        )
        memory = parse_prop(props, "MemoryCurrent")
        limit = parse_prop(props, "MemoryMax")
        if memory and limit and limit > 0:
            line = f"память {memory / 2**20:.0f}M из {limit / 2**20:.0f}M"
            if memory / limit > 0.95:
                self.report_failure(f"память на пределе: {line}")
            self.ok(line)
        self.note(f"рестартов с запуска сервиса: {parse_prop(props, 'NRestarts')}")
        journal = self.ssh(f"journalctl -u {self.service} --since '-5 min' --no-pager", check=False)
        found = parse_journal_version(journal)
        if found is None:
            self.report_failure("в журнале нет строки старта «Gatecheck Bot v…» — бот не поднялся")
        if found != self.version:
            self.report_failure(f"бот отчитался версией v{found}, ожидалась v{self.version}")
        self.ok(f"в журнале версия v{found} совпала с локальной")
        self.journal_errors = [
            line for line in journal.splitlines() if "| ERROR" in line or "Traceback" in line
        ]
        for line in self.journal_errors[-5:]:
            self.note(f"ошибка в журнале: {line.strip()}")

    def runuser_prefix(self) -> str:
        return f"runuser -u {self.run_as} -- " if self.use_runuser else ""


    def report_failure(self, reason: str) -> None:
        self.step(f"{FAIL} Верификация не прошла: {reason}")
        manual = (
            f"ssh {self.target} \"{self.runuser_prefix()}git -C {self.path} "
            f"reset --hard {self.old_sha} && systemctl restart {self.service}\""
        )
        self.note(f"откат вручную: {manual}")
        if self.args.rollback_on_fail:
            self.note("выполняю авто-откат…")
            self.remote_git(f"reset --hard {self.old_sha}", check=False)
            self.ssh(f"systemctl restart {self.service}", check=False)
            time.sleep(self.args.wait)
            self.note(f"откатились на {self.old_sha[:7]} — проверь журнал")
        raise SystemExit(f"{FAIL} Деплой не подтверждён: {reason}")

    def run(self) -> None:
        self.check_local_repo()
        self.check_server()
        self.run_local_checks()
        self.push()
        if not self.check_remote_lag():
            return
        if self.dry_run:
            self.step(
                f"DRY-RUN: дальше были бы fetch/reset до {self.new_sha[:7]}, pip при изменениях, "
                f"restart {self.service} и верификация (версия v{self.version}, память, журнал)"
            )
            return
        self.update_code()
        self.restart()
        self.verify()
        if self.journal_errors and self.args.strict_errors:
            raise SystemExit(f"{FAIL} В журнале есть ошибки (--strict-errors).")
        print(f"\n{OK} Деплой v{self.version} на {self.target} завершён и проверен.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Деплой Gatecheck Bot на прод с обязательными проверками "
        "(см. deploy/README.md).",
    )
    parser.add_argument("--dry-run", action="store_true", help="только проверки, прод не трогаем")
    parser.add_argument("--no-push", action="store_true", help="не пушить в origin")
    parser.add_argument("--no-tests", action="store_true", help="пропустить ruff+pytest")
    parser.add_argument("--allow-dirty", action="store_true", help="не требовать чистое дерево")
    parser.add_argument(
        "--force-restart", action="store_true", help="рестарт, даже если код не менялся"
    )
    parser.add_argument(
        "--strict-errors", action="store_true", help="ошибки в журнале считать провалом"
    )
    parser.add_argument(
        "--rollback-on-fail", action="store_true", help="авто-откат на прежний коммит при провале"
    )
    parser.add_argument(
        "--wait", type=float, default=6.0, help="пауза после рестарта, с (по умолчанию 6)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows-консоль по умолчанию может быть не в UTF-8 — иначе падаем на ✓ и кириллице.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    deployer = Deployer(args)
    mode = " [dry-run]" if args.dry_run else ""
    print(
        f"Gatecheck Bot deploy{mode}: target={deployer.target}, path={deployer.path}, "
        f"service={deployer.service}, версия v{deployer.version}"
    )
    deployer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

