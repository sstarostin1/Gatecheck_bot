"""Тесты модуля маршрутов: имя→ID, BFS, форматирование, загрузка графа."""

import json
from pathlib import Path

import pytest

from gatecheck_bot.routing import (
    Graph,
    build_name_index,
    find_route,
    format_route,
    load_graph,
    resolve_system,
)


@pytest.fixture()
def graph() -> Graph:
    systems = {
        "1": {"name": "Alpha", "constellation_id": 10, "security_status": 0.5},
        "2": {"name": "Beta", "constellation_id": 10, "security_status": 0.3},
        "3": {"name": "Gamma", "constellation_id": 10, "security_status": -0.2},
        "4": {"name": "Delta", "constellation_id": 10, "security_status": -0.5},
        "5": {"name": "Island", "constellation_id": 10, "security_status": 0.1},
    }
    adjacency = {"1": ["2"], "2": ["1", "3"], "3": ["2", "4"], "4": ["3"]}
    return Graph(systems=systems, adjacency=adjacency, meta={})


def test_find_route_three_jumps(graph: Graph) -> None:
    assert find_route(graph.adjacency, "1", "4") == ["1", "2", "3", "4"]


def test_find_route_same_system(graph: Graph) -> None:
    assert find_route(graph.adjacency, "1", "1") == ["1"]


def test_find_route_no_path_to_island(graph: Graph) -> None:
    assert find_route(graph.adjacency, "1", "5") is None


def test_name_index_and_resolve(graph: Graph) -> None:
    index = build_name_index(graph)
    assert resolve_system("  Alpha ", index) == "1"
    assert resolve_system("nope", index) is None


def test_format_route(graph: Graph) -> None:
    assert format_route(graph, ["1", "2", "3"]) == "Alpha → Beta → Gamma"


def test_load_graph_missing_file(tmp_path: Path) -> None:
    assert load_graph(tmp_path / "nope.json") is None


def test_load_graph_valid(tmp_path: Path) -> None:
    doc = {
        "meta": {"version": 1},
        "systems": {"1": {"name": "Alpha"}},
        "adjacency": {"1": ["2"], "2": []},
    }
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    graph = load_graph(path)
    assert graph is not None
    assert graph.name_of("1") == "Alpha"
    assert graph.name_of("999") == "999"


def test_build_route_reply_happy(graph: Graph) -> None:
    from gatecheck_bot.handlers import build_route_reply

    text, route = build_route_reply(graph, "Alpha Delta")
    assert "Alpha → Beta → Gamma → Delta" in text
    assert route == ["1", "2", "3", "4"]
    assert "Прыжков: 3" in text


def test_build_route_reply_errors(graph: Graph) -> None:
    from gatecheck_bot.handlers import build_route_reply

    text, route = build_route_reply(graph, "")
    assert "Формат" in text and route is None
    text, route = build_route_reply(graph, "Alpha Beta Gamma")
    assert "ровно две" in text and route is None
    text, route = build_route_reply(graph, "Alpha Nope")
    assert "Не нашёл" in text and route is None


def test_build_route_reply_same_system(graph: Graph) -> None:
    from gatecheck_bot.handlers import build_route_reply

    text, route = build_route_reply(graph, "Alpha Alpha")
    assert "уже там" in text and route is None  # слежение одной системы не запускается


def test_build_route_reply_arrow_separator(graph: Graph) -> None:
    from gatecheck_bot.handlers import build_route_reply

    text, route = build_route_reply(graph, "Alpha → Beta")
    assert "Прыжков: 1" in text and route == ["1", "2"]