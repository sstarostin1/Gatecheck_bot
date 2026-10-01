"""Тесты рендеринга v0.11: приписка капсул (§0.13), футеры репортов (§0.4),
HTML-ссылки (§0.11: markdown-форма выводится как есть — баг ≤ v0.11.0)."""

import re
from pathlib import Path

from gatecheck_bot.render import (
    capsules_note,
    capsules_word,
    route_footer,
    zone_footer,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "gatecheck_bot"


def test_capsules_word_pluralization() -> None:
    assert capsules_word(1) == "капсула"
    assert capsules_word(2) == "капсулы"
    assert capsules_word(4) == "капсулы"
    assert capsules_word(5) == "капсул"
    assert capsules_word(11) == "капсул"
    assert capsules_word(21) == "капсула"
    assert capsules_word(112) == "капсул"


def test_capsules_note_is_empty_for_zero() -> None:
    assert capsules_note(0) == ""  # нет капсул — линии не раздуваем (§0.13)
    assert capsules_note(1) == " (+1 капсула)"
    assert capsules_note(3) == " (+3 капсулы)"
    assert capsules_note(12) == " (+12 капсул)"


def test_footers_mention_capsule_exclusion() -> None:
    zone = zone_footer()
    assert "без подбитых капсул" in zone
    assert "их импланты не выпадают" in zone
    assert zone.startswith("<i>") and zone.endswith("</i>")
    route = route_footer()
    assert "без подбитых капсул" in route
    assert "/route_stop" in route
    assert route.startswith("<i>") and route.endswith("</i>")


def test_no_markdown_links_in_bot_sources() -> None:
    """§0.11: в HTML parse mode `[текст](url)` печатается как есть — ссылки только <a href>."""
    pattern = re.compile(r"\]\(https?://")
    offenders = [
        f"{path.name}:{i}"
        for path in sorted(PACKAGE_DIR.glob("*.py"))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == [], f"markdown-ссылки в текстах бота: {offenders}"


def test_user_facing_links_are_html() -> None:
    handlers = (PACKAGE_DIR / "handlers.py").read_text(encoding="utf-8")
    assert '<a href="https://eve-gatecheck.space/">сервисом</a>' in handlers
    assert '<a href="https://github.com/sstarostin1/Gatecheck_bot">' in handlers