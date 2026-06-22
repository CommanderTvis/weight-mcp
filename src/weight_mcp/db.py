"""SQLite persistence.

Single user, self-hosted, low write volume -> SQLite is the right default:
one file, zero ops, backup = copy the file. Datetimes are stored as ISO-8601
strings in the server's local time (the self-hoster runs it in their own zone).
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import DayTotals, FoodLog, Goals, WeightEntry

_SCHEMA = """
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

-- OAuth Dynamic Client Registration records. Persisted so claude.ai's stored
-- registration keeps working across server restarts. (Tokens are stateless
-- JWTs and auth codes are ephemeral, so neither needs a table.)
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    info_json TEXT NOT NULL
);

-- The single live goals row (seeded from env on first run, then editable).
CREATE TABLE IF NOT EXISTS goals (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    goal_mode           TEXT    NOT NULL,
    calorie_target_kcal INTEGER NOT NULL,
    protein_target_g    INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- weight -------------------------------------------------------------

    def add_weight(self, weight_kg: float, recorded_at: datetime | None = None) -> WeightEntry:
        when = recorded_at or datetime.now()
        cur = self._conn.execute(
            "INSERT INTO weight_entries (recorded_at, weight_kg) VALUES (?, ?)",
            (when.isoformat(), weight_kg),
        )
        self._conn.commit()
        return WeightEntry(id=int(cur.lastrowid or 0), recorded_at=when, weight_kg=weight_kg)

    def weight_series(self, limit: int = 365) -> list[WeightEntry]:
        rows = self._conn.execute(
            "SELECT id, recorded_at, weight_kg FROM weight_entries "
            "ORDER BY recorded_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        entries = [
            WeightEntry(
                id=r["id"],
                recorded_at=datetime.fromisoformat(r["recorded_at"]),
                weight_kg=r["weight_kg"],
            )
            for r in rows
        ]
        entries.reverse()  # chronological for plotting
        return entries

    # --- food ---------------------------------------------------------------

    def add_food_log(
        self,
        *,
        name: str,
        kcal: float,
        protein_g: float,
        quantity_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
        source: str | None = None,
        eaten_at: datetime | None = None,
    ) -> FoodLog:
        when = eaten_at or datetime.now()
        cur = self._conn.execute(
            "INSERT INTO food_logs "
            "(eaten_at, name, quantity_g, kcal, protein_g, carbs_g, fat_g, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (when.isoformat(), name, quantity_g, kcal, protein_g, carbs_g, fat_g, source),
        )
        self._conn.commit()
        return FoodLog(
            id=int(cur.lastrowid or 0),
            eaten_at=when,
            name=name,
            quantity_g=quantity_g,
            kcal=kcal,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            source=source,
        )

    def recent_food_logs(self, limit: int = 20) -> list[FoodLog]:
        rows = self._conn.execute(
            "SELECT id, eaten_at, name, quantity_g, kcal, protein_g, carbs_g, fat_g, source "
            "FROM food_logs ORDER BY eaten_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            FoodLog(
                id=r["id"],
                eaten_at=datetime.fromisoformat(r["eaten_at"]),
                name=r["name"],
                quantity_g=r["quantity_g"],
                kcal=r["kcal"],
                protein_g=r["protein_g"],
                carbs_g=r["carbs_g"],
                fat_g=r["fat_g"],
                source=r["source"],
            )
            for r in rows
        ]

    # --- oauth clients ------------------------------------------------------

    def add_oauth_client(self, client_id: str, info_json: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, info_json) VALUES (?, ?)",
            (client_id, info_json),
        )
        self._conn.commit()

    def get_oauth_client(self, client_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT info_json FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return row["info_json"] if row else None

    # --- goals --------------------------------------------------------------

    def get_goals(self) -> Goals | None:
        row = self._conn.execute(
            "SELECT goal_mode, calorie_target_kcal, protein_target_g FROM goals WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return Goals(
            goal_mode=row["goal_mode"],
            calorie_target_kcal=row["calorie_target_kcal"],
            protein_target_g=row["protein_target_g"],
        )

    def save_goals(self, goals: Goals) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO goals "
            "(id, goal_mode, calorie_target_kcal, protein_target_g) VALUES (1, ?, ?, ?)",
            (goals.goal_mode, goals.calorie_target_kcal, goals.protein_target_g),
        )
        self._conn.commit()

    def day_totals(self, day: date) -> DayTotals:
        prefix = day.isoformat()  # eaten_at starts with YYYY-MM-DD for that day
        row = self._conn.execute(
            "SELECT "
            "  COALESCE(SUM(kcal), 0)      AS kcal, "
            "  COALESCE(SUM(protein_g), 0) AS protein_g, "
            "  COALESCE(SUM(carbs_g), 0)   AS carbs_g, "
            "  COALESCE(SUM(fat_g), 0)     AS fat_g, "
            "  COUNT(*)                    AS item_count "
            "FROM food_logs WHERE eaten_at LIKE ?",
            (f"{prefix}%",),
        ).fetchone()
        return DayTotals(
            day=day,
            kcal=row["kcal"],
            protein_g=row["protein_g"],
            carbs_g=row["carbs_g"],
            fat_g=row["fat_g"],
            item_count=row["item_count"],
        )
