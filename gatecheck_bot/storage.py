"""SQLite-персистентность (D2, VISION §8): подписки, маршруты, kill-кэш, лог алертов.

Один файл (по умолчанию data/gatecheck.sqlite3), WAL, busy_timeout. Простые .sql-миграции
лежат в gatecheck_bot/migrations/ и применяются по порядку; применённые фиксируются в
_migrations. Операции короткие (единицы строк) — вызываются прямо из event loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("gatecheck_bot.storage")


class Storage:
    """Тонкая обёртка над sqlite3: схема из миграций + CRUD для мониторов."""

    def __init__(self, path: str | Path = "data/gatecheck.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        applied = {row["name"] for row in self._conn.execute("SELECT name FROM _migrations")}
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in applied:
                continue
            logger.info("Применяю миграцию %s", path.name)
            self._conn.executescript(path.read_text(encoding="utf-8"))
            self._conn.execute("INSERT INTO _migrations (name) VALUES (?)", (path.name,))
            self._conn.commit()
            applied.add(path.name)

    def close(self) -> None:
        self._conn.close()

    # --- пользователи и настройки (OQ-8, /settings) -----------------------

    def upsert_user(self, chat_id: int) -> None:
        self._conn.execute(
            "INSERT INTO users (chat_id) VALUES (?) ON CONFLICT (chat_id) DO NOTHING",
            (chat_id,),
        )
        self._conn.commit()

    def set_settings(self, chat_id: int, settings: dict) -> None:
        self.upsert_user(chat_id)
        self._conn.execute(
            "UPDATE users SET settings_json = ? WHERE chat_id = ?",
            (json.dumps(settings), chat_id),
        )
        self._conn.commit()

    def get_settings(self, chat_id: int) -> dict:
        row = self._conn.execute(
            "SELECT settings_json FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["settings_json"])
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # --- зона --------------------------------------------------------------

    def add_zone_sub(self, chat_id: int) -> None:
        self._conn.execute(
            "INSERT INTO zone_subs (chat_id) VALUES (?) ON CONFLICT (chat_id) DO NOTHING",
            (chat_id,),
        )
        self._conn.commit()

    def del_zone_sub(self, chat_id: int) -> None:
        self._conn.execute("DELETE FROM zone_subs WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    def zone_sub_ids(self) -> list[int]:
        return [row["chat_id"] for row in self._conn.execute("SELECT chat_id FROM zone_subs")]

    # --- маршруты ------------------------------------------------------------

    def upsert_route(
        self, chat_id: int, route: list[str], started_epoch: float, expires_epoch: float
    ) -> None:
        self._conn.execute(
            "INSERT INTO route_watches (chat_id, route_json, started_epoch, expires_epoch) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (chat_id) DO UPDATE SET "
            "route_json = excluded.route_json, started_epoch = excluded.started_epoch, "
            "expires_epoch = excluded.expires_epoch",
            (chat_id, json.dumps(route), started_epoch, expires_epoch),
        )
        self._conn.commit()

    def del_route(self, chat_id: int) -> None:
        self._conn.execute("DELETE FROM route_watches WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    def active_routes(self, now_epoch: float) -> list[dict]:
        rows = self._conn.execute(
            "SELECT chat_id, route_json, started_epoch, expires_epoch "
            "FROM route_watches WHERE expires_epoch > ?",
            (now_epoch,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                route = json.loads(row["route_json"])
            except ValueError:
                continue
            result.append(
                {
                    "chat_id": int(row["chat_id"]),
                    "route": [str(sid) for sid in route],
                    "started_epoch": float(row["started_epoch"]),
                    "expires_epoch": float(row["expires_epoch"]),
                }
            )
        return result

    # --- kill-кэш --------------------------------------------------------------

    def add_kill_event(
        self,
        system_id: str,
        kill_id: int,
        gate_id: int,
        kill_ts: float,
        droppable: float,
        ship_type_id: int | None,
        features: dict | None = None,
    ) -> None:
        """Сохранить килл «на гейте» (+ признаки v0.10 для восстановления после рестарта)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO kill_events "
            "(kill_id, system_id, gate_id, kill_ts, droppable, ship_type_id, features_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                kill_id,
                system_id,
                gate_id,
                kill_ts,
                droppable,
                ship_type_id,
                json.dumps(features) if features else None,
            ),
        )
        self._conn.commit()

    def kill_events_since(self, since_epoch: float) -> list[dict]:
        rows = self._conn.execute(
            "SELECT system_id, kill_id, gate_id, kill_ts, droppable, ship_type_id, features_json "
            "FROM kill_events WHERE kill_ts >= ? ORDER BY kill_ts",
            (since_epoch,),
        ).fetchall()
        result = []
        for row in rows:
            features = None
            raw = row["features_json"]
            if raw:
                try:
                    data = json.loads(raw)
                    features = data if isinstance(data, dict) else None
                except ValueError:
                    features = None
            result.append(
                {
                    "system_id": str(row["system_id"]),
                    "kill_id": int(row["kill_id"]),
                    "gate_id": int(row["gate_id"]),
                    "kill_ts": float(row["kill_ts"]),
                    "droppable": float(row["droppable"]),
                    "ship_type_id": row["ship_type_id"],
                    "features": features,
                }
            )
        return result

    def prune_kill_events(self, before_epoch: float) -> None:
        self._conn.execute("DELETE FROM kill_events WHERE kill_ts < ?", (before_epoch,))
        self._conn.commit()

    # --- алерты -----------------------------------------------------------------

    def log_alert(self, chat_id: int, kind: str, system_id: str, sent_epoch: float) -> None:
        self._conn.execute(
            "INSERT INTO alerts_log (chat_id, kind, system_id, sent_epoch) VALUES (?, ?, ?, ?)",
            (chat_id, kind, system_id, sent_epoch),
        )
        self._conn.commit()

    def last_alert_epochs(self, kind: str) -> dict[tuple[int, str], float]:
        """Последнее время алерта (epoch) по (chat_id, system_id) для данного типа."""
        rows = self._conn.execute(
            "SELECT chat_id, system_id, MAX(sent_epoch) AS last_epoch "
            "FROM alerts_log WHERE kind = ? GROUP BY chat_id, system_id",
            (kind,),
        ).fetchall()
        return {
            (int(row["chat_id"]), str(row["system_id"])): float(row["last_epoch"])
            for row in rows
        }