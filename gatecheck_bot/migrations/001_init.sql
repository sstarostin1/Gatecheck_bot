-- 001_init: базовая схема (D2, VISION §8): пользователи+настройки, подписки на зону,
-- маршруты (TTL), kill-кэш (киллы на гейтах), лог алертов (cooldown/отладка).
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS zone_subs (
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS route_watches (
    chat_id INTEGER PRIMARY KEY,
    route_json TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    expires_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kill_events (
    kill_id INTEGER PRIMARY KEY,
    system_id TEXT NOT NULL,
    gate_id INTEGER NOT NULL,
    kill_ts REAL NOT NULL,
    droppable REAL NOT NULL DEFAULT 0,
    ship_type_id INTEGER,
    inserted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kill_events_kill_ts ON kill_events (kill_ts);

CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    system_id TEXT NOT NULL,
    sent_epoch REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_kind_chat ON alerts_log (kind, chat_id, sent_epoch);
