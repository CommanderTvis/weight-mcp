"""Versioned, forward-only SQLite migrations.

Applied on startup, tracked by ``PRAGMA user_version`` (the number of migrations
that have run). Each step runs at most once, in order, and is idempotent so a
partial/failed run is safe to re-apply. To change the schema, **append** a new
function to ``MIGRATIONS`` — never edit or reorder a shipped one.
"""

import sqlite3


def _001_baseline(conn: sqlite3.Connection) -> None:
    """The original schema. ``IF NOT EXISTS`` makes this a no-op on a DB that
    already has these tables (so existing data is untouched)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weight_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT    NOT NULL,
            weight_kg   REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS food_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            eaten_at   TEXT    NOT NULL,
            name       TEXT    NOT NULL,
            quantity_g REAL,
            kcal       REAL    NOT NULL,
            protein_g  REAL    NOT NULL,
            carbs_g    REAL,
            fat_g      REAL,
            source     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_food_logs_eaten_at ON food_logs (eaten_at);
        CREATE INDEX IF NOT EXISTS idx_weight_recorded_at ON weight_entries (recorded_at);
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            info_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS goals (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            goal_mode           TEXT    NOT NULL,
            calorie_target_kcal INTEGER NOT NULL,
            protein_target_g    INTEGER NOT NULL
        );
        """
    )


def _002_meal_numbers(conn: sqlite3.Connection) -> None:
    """Per-day meal numbers for idempotent edits. Adds the columns if missing,
    backfills the day for existing rows (their meal_number stays NULL — they
    predate numbering and are left intact), and enforces one row per (day, number)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(food_logs)")}
    if "eaten_day" not in columns:
        conn.execute("ALTER TABLE food_logs ADD COLUMN eaten_day TEXT")
    if "meal_number" not in columns:
        conn.execute("ALTER TABLE food_logs ADD COLUMN meal_number INTEGER")
    conn.execute("UPDATE food_logs SET eaten_day = substr(eaten_at, 1, 10) WHERE eaten_day IS NULL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_food_day_number "
        "ON food_logs (eaten_day, meal_number)"
    )


MIGRATIONS = [_001_baseline, _002_meal_numbers]


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, step in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        step(conn)
        # user_version takes a literal, not a bound param; version is a trusted int.
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
