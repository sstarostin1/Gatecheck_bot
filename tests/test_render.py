"""Тесты рендеринга v0.11: приписка капсул (§0.13) и футеры репортов (§0.4)."""

from gatecheck_bot.render import (
    capsules_note,
    capsules_word,
    route_footer,
    zone_footer,
)


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