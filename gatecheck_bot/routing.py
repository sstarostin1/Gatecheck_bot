"""Поиск маршрутов: статический граф систем + BFS по классическим гейтам (M1).

Граф строится скриптом scripts/fetch_static.py из ESI и лежит в data/graph.json.
BFS достаточен: все прыжки между системами весят одинаково, граф ~десятки узлов.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Graph:
    """Статический граф региона: системы и смежность через классические гейты."""

    systems: dict[str, dict]  # system_id -> {name, constellation_id, security_status}
    adjacency: dict[str, list[str]]  # system_id -> [system_id, ...]
    meta: dict

    def name_of(self, system_id: str) -> str:
        info = self.systems.get(system_id) or {}
        return str(info.get("name", system_id))


def load_graph(path: str | Path) -> Graph | None:
    """Загрузить graph.json; отсутствующий/битый файл → None (не фатально)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    systems = data.get("systems") or {}
    adjacency = data.get("adjacency") or {}
    if not isinstance(systems, dict) or not isinstance(adjacency, dict):
        return None
    if not systems or not adjacency:
        return None
    return Graph(systems=systems, adjacency=adjacency, meta=data.get("meta", {}))


def build_name_index(graph: Graph) -> dict[str, str]:
    """Индекс: имя в нижнем регистре -> system_id (имена систем в EVE уникальны)."""
    return {
        str(info.get("name", "")).strip().lower(): system_id
        for system_id, info in graph.systems.items()
        if info.get("name")
    }


def resolve_system(query: str, index: dict[str, str]) -> str | None:
    """Найти system_id по пользовательской строке (без учёта регистра)."""
    return index.get(query.strip().lower())


def find_route(
    adjacency: dict[str, list[str]], start: str, goal: str
) -> list[str] | None:
    """Кратчайший маршрут start→goal (BFS); нет пути → None; start==goal → [start]."""
    if start == goal:
        return [start]
    parent: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, ()):
            if neighbor in parent:
                continue
            parent[neighbor] = current
            if neighbor == goal:
                path = [goal]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                return list(reversed(path))
            queue.append(neighbor)
    return None


def format_route(graph: Graph, route: list[str]) -> str:
    """Маршрут одной строкой: Amamake → Vard → Siseide."""
    return " → ".join(graph.name_of(system_id) for system_id in route)