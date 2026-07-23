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


def _003_backfill_meal_numbers(conn: sqlite3.Connection) -> None:
    """Give a meal_number to rows that predate numbering (left NULL by _002).
    A NULL-numbered row still counts toward the day's totals but can't be
    targeted by ``delete_food``/``log_food``, which match on the number — so it
    can never be removed or edited. Assign each such row the next free number
    for its day, oldest first, matching how new logs are numbered."""
    days = [
        row["eaten_day"]
        for row in conn.execute(
            "SELECT DISTINCT eaten_day FROM food_logs WHERE meal_number IS NULL"
        )
    ]
    for day in days:
        nxt = (
            conn.execute(
                "SELECT COALESCE(MAX(meal_number), 0) AS m FROM food_logs WHERE eaten_day = ?",
                (day,),
            ).fetchone()["m"]
            + 1
        )
        rows = conn.execute(
            "SELECT id FROM food_logs WHERE eaten_day = ? AND meal_number IS NULL "
            "ORDER BY eaten_at, id",
            (day,),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE food_logs SET meal_number = ? WHERE id = ?", (nxt, row["id"]))
            nxt += 1


def _004_users(conn: sqlite3.Connection) -> None:
    """Multi-user: a ``users`` table for the non-admin accounts the admin
    registers, and a ``username`` column scoping every data row. All data that
    exists before this migration belongs to the admin account ("admin"), whose
    password still lives in the environment, not in this table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        )
        """
    )
    for table in ("weight_entries", "food_logs"):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
        if "username" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN username TEXT NOT NULL DEFAULT 'admin'")  # noqa: S608
    # Meal numbers count per user per day now.
    conn.execute("DROP INDEX IF EXISTS idx_food_day_number")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_food_user_day_number "
        "ON food_logs (username, eaten_day, meal_number)"
    )
    # Goals go from one global row (id = 1) to one row per username. The rename
    # dance is guarded so a partially-applied run (crash mid-step) re-applies
    # cleanly: whatever still sits in goals_v1 is migrated, then dropped.
    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "goals" in tables:
        goal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(goals)")}
        if "username" in goal_columns:
            return  # already migrated
        conn.execute("ALTER TABLE goals RENAME TO goals_v1")
    conn.execute(
        """
        CREATE TABLE goals (
            username            TEXT PRIMARY KEY,
            goal_mode           TEXT    NOT NULL,
            calorie_target_kcal INTEGER NOT NULL,
            protein_target_g    INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO goals (username, goal_mode, calorie_target_kcal, protein_target_g) "
        "SELECT 'admin', goal_mode, calorie_target_kcal, protein_target_g FROM goals_v1"
    )
    conn.execute("DROP TABLE goals_v1")


def _005_fiber(conn: sqlite3.Connection) -> None:
    """Optional fiber tracking: a per-meal ``fiber_g`` and a per-user
    ``fiber_target_g`` norm. Both nullable — NULL means "not tracked" for a
    meal and "no norm set" for goals."""
    for table, column in (("food_logs", "fiber_g"), ("goals", "fiber_target_g")):
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")  # noqa: S608


MIGRATIONS = [_001_baseline, _002_meal_numbers, _003_backfill_meal_numbers, _004_users, _005_fiber]


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, step in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        step(conn)
        # user_version takes a literal, not a bound param; version is a trusted int.
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
