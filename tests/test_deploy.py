"""Тесты скрипта деплоя (scripts/deploy.py): чистые хелперы без сети/ssh."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_deploy():
    """Загрузить scripts/deploy.py как модуль (scripts/ не пакет)."""
    spec = importlib.util.spec_from_file_location("deploy", ROOT / "scripts" / "deploy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_version_matches_package() -> None:
    import gatecheck_bot

    deploy = load_deploy()
    assert deploy.read_version() == gatecheck_bot.__version__


def test_parse_journal_version_takes_last_startup() -> None:
    deploy = load_deploy()
    journal = (
        "Sep 24 python[899]: Gatecheck Bot v0.10.0 запущен как @Eve_gatechecker_bot\n"
        "Sep 29 python[899]: Gatecheck Bot v0.11.1 запущен как @Eve_gatechecker_bot"
    )
    assert deploy.parse_journal_version(journal) == "0.11.1"
    assert deploy.parse_journal_version("ничего интересного") is None


def test_ssh_target_prefers_host_over_alias() -> None:
    deploy = load_deploy()
    assert deploy.ssh_target({"VPS_HOST": "1.2.3.4", "VPS_USER": "gatecheck"}) == "gatecheck@1.2.3.4"
    assert deploy.ssh_target({"VPS_HOST": "1.2.3.4"}) == "root@1.2.3.4"
    assert deploy.ssh_target({}) == "vps"
    assert deploy.ssh_target({"VPS_ALIAS": "prod-box"}) == "prod-box"


def test_parse_prop_reads_systemctl_values() -> None:
    deploy = load_deploy()
    props = "MemoryCurrent=227041280\nMemoryMax=419430400\nNRestarts=0"
    assert deploy.parse_prop(props, "MemoryCurrent") == 227041280
    assert deploy.parse_prop(props, "MemoryMax") == 419430400
    assert deploy.parse_prop(props, "MemoryPeak") == 0
    # Пакетный сценарий отдаёт свойства одной строкой (после tr '\n' ' ').
    joined = "MemoryCurrent=227041280 MemoryMax=419430400 NRestarts=3"
    assert deploy.parse_prop(joined, "MemoryCurrent") == 227041280
    assert deploy.parse_prop(joined, "NRestarts") == 3


def test_dry_run_flag_parses() -> None:
    deploy = load_deploy()
    args = deploy.parse_args(["--dry-run", "--wait", "3"])
    assert args.dry_run is True and args.wait == 3.0 and args.no_tests is False
