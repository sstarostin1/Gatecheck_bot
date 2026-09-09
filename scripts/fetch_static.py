"""Сборка статических данных EVE: граф систем/гейтов региона из ESI (M1).

Запуск:
    .venv/Scripts/python scripts/fetch_static.py [--region 10000030]

Выход:
    data/graph.json — системы + смежность (для BFS-маршрутов /route);
    data/gates.json — гейты с именами/координатами (пригодятся M2/M3:
    «килл на воротах» = zkb.locationID ∈ itemID гейтов, см. spike-01.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

BASE_DIR = Path(__file__).resolve().parent.parent
ESI = "https://esi.evetech.net/latest"
UA = "GatecheckBot/0.3 (+https://github.com/Anewkey/gatecheck-bot)"
CONCURRENCY = 8
RETRIES = 3

logger = logging.getLogger("fetch_static")


async def get_json(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    """GET c ретраями; 404 → None (сущности нет), прочие сбои → None после ретраев."""
    for attempt in range(RETRIES):
        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    logger.warning("404: %s", url)
                    return None
                resp.raise_for_status()
                return await resp.json()
        except Exception as exc:
            if attempt == RETRIES - 1:
                logger.warning("Сбой %s: %s: %s", url, type(exc).__name__, exc)
                return None
            await asyncio.sleep(1.0 + attempt)
    return None


async def fetch_many(
    session: aiohttp.ClientSession, urls: list[str]
) -> list[dict | list | None]:
    """Параллельные запросы с ограничением concurrency (этикет ESI)."""
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def _one(url: str):
        async with semaphore:
            return await get_json(session, url)

    return list(await asyncio.gather(*(_one(url) for url in urls)))


async def build_graph(region_id: int, out_dir: Path) -> None:
    async with aiohttp.ClientSession(
        headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        logger.info("Регион %s: запрашиваю созвездия...", region_id)
        region = await get_json(session, f"{ESI}/universe/regions/{region_id}/")
        if not region:
            raise SystemExit(f"Регион {region_id} не найден в ESI.")
        constellation_ids: list[int] = list(region["constellations"])

        logger.info("Созвездий: %d — запрашиваю системы...", len(constellation_ids))
        constellations = await fetch_many(
            session,
            [f"{ESI}/universe/constellations/{cid}/" for cid in constellation_ids],
        )
        system_ids: list[int] = sorted(
            {sid for c in constellations if c for sid in c.get("systems", [])}
        )
        logger.info("Систем в регионе: %d — запрашиваю детали...", len(system_ids))

        system_payloads = await fetch_many(
            session, [f"{ESI}/universe/systems/{sid}/" for sid in system_ids]
        )
        systems: dict[str, dict] = {}
        gate_ids: list[int] = []
        for sid, payload in zip(system_ids, system_payloads):
            if not payload:
                continue
            systems[str(sid)] = {
                "name": payload["name"],
                "constellation_id": payload["constellation_id"],
                "security_status": round(payload.get("security_status") or 0.0, 4),
            }
            gate_ids.extend(payload.get("stargates", []))

        logger.info("Гейтов: %d — запрашиваю назначения...", len(gate_ids))
        gate_payloads = await fetch_many(
            session, [f"{ESI}/universe/stargates/{gid}/" for gid in gate_ids]
        )
        gates: dict[str, dict] = {}
        adjacency: dict[str, set[str]] = {sid: set() for sid in systems}
        for gid, payload in zip(gate_ids, gate_payloads):
            if not payload:
                continue
            src = str(payload["system_id"])
            dest = str(payload["destination"]["system_id"])
            gates[str(gid)] = {
                "system_id": src,
                "dest_system_id": dest,
                "name": payload.get("name", ""),
                "position": payload.get("position", {}),
            }
            # Смежность — только внутри набора систем графа (маршруты v1 — в регионе).
            if src in systems and dest in systems:
                adjacency[src].add(dest)

    meta = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "region_id": region_id,
        "region_name": region["name"],
        "esi_base": ESI,
        "counts": {
            "systems": len(systems),
            "gates": len(gates),
            "adjacency_edges": sum(len(v) for v in adjacency.values()),
        },
    }
    graph_doc = {
        "meta": meta,
        "systems": systems,
        "adjacency": {sid: sorted(dests) for sid, dests in adjacency.items()},
    }
    gates_doc = {"meta": meta, "gates": gates}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text(
        json.dumps(graph_doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / "gates.json").write_text(
        json.dumps(gates_doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    logger.info(
        "Готово: graph.json (%d систем), gates.json (%d гейтов) → %s",
        len(systems), len(gates), out_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка графа гейтов региона из ESI.")
    parser.add_argument(
        "--region", type=int, default=10000030, help="region_id (по умолчанию Heimatar)"
    )
    parser.add_argument("--out", type=Path, default=BASE_DIR / "data", help="каталог вывода")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    asyncio.run(build_graph(args.region, args.out))


if __name__ == "__main__":
    main()