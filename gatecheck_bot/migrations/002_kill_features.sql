-- 002: признаки килла (v0.10, спека docs/MESSAGES.md §7) — атака, бомбы/бабблы,
-- состав атакующих. JSON-поля ради SQLite-персистентности после рестарта.
ALTER TABLE kill_events ADD COLUMN features_json TEXT;
