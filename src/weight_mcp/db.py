"""SQLite persistence.

A handful of users, self-hosted, low write volume -> SQLite is the right
default: one file, zero ops, backup = copy the file. Datetimes are stored as
ISO-8601 strings in the server's local time (the self-hoster runs it in their
own zone).

Every data row is scoped by ``username``. The admin account ("admin") is not a
row in ``users`` — its password lives in the environment — but its data rows
carry the username like everyone else's. ``users`` holds only the non-admin
accounts the admin registers, with salted PBKDF2 password hashes.
"""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .migrations import migrate
from .models import DayTotals, FoodLog, Goals, WeightEntry

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Salted PBKDF2-SHA256, self-describing: ``pbkdf2$<iterations>$<salt>$<hash>``."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    # --- users (non-admin accounts) -----------------------------------------

    def create_user(self, username: str, password_hash: str) -> bool:
        """Register a non-admin account. Returns False if the name is taken."""
        try:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, password_hash, datetime.now().isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
        self._conn.commit()
        return True

    def delete_user(self, username: str) -> bool:
        """Remove a non-admin account. Their logged data stays (re-registering
        the same username reattaches it). Returns whether the account existed."""
        cur = self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
        self._conn.commit()
        return cur.rowcount > 0

    def set_user_password(self, username: str, password_hash: str) -> bool:
        cur = self._conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_user_password_hash(self, username: str) -> str | None:
        row = self._conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row["password_hash"] if row else None

    def list_users(self) -> list[str]:
        rows = self._conn.execute("SELECT username FROM users ORDER BY username").fetchall()
        return [r["username"] for r in rows]

    # --- weight -------------------------------------------------------------

    def add_weight(
        self, username: str, weight_kg: float, recorded_at: datetime | None = None
    ) -> WeightEntry:
        when = recorded_at or datetime.now()
        cur = self._conn.execute(
            "INSERT INTO weight_entries (username, recorded_at, weight_kg) VALUES (?, ?, ?)",
            (username, when.isoformat(), weight_kg),
        )
        self._conn.commit()
        return WeightEntry(id=int(cur.lastrowid or 0), recorded_at=when, weight_kg=weight_kg)

    def weight_series(self, username: str, limit: int = 365) -> list[WeightEntry]:
        rows = self._conn.execute(
            "SELECT id, recorded_at, weight_kg FROM weight_entries WHERE username = ? "
            "ORDER BY recorded_at DESC LIMIT ?",
            (username, limit),
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
        username: str,
        *,
        name: str,
        kcal: float,
        protein_g: float,
        quantity_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
        fiber_g: float | None = None,
        source: str | None = None,
        eaten_at: datetime | None = None,
        meal_number: int | None = None,
    ) -> FoodLog:
        """Log a food item. Pass ``meal_number`` to overwrite that meal of the day
        (idempotent edit); omit it to append as the next meal of the day."""
        when = eaten_at or datetime.now()
        day = when.date().isoformat()
        if meal_number is None:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(meal_number), 0) AS m FROM food_logs "
                "WHERE username = ? AND eaten_day = ?",
                (username, day),
            ).fetchone()
            meal_number = int(row["m"]) + 1
        self._conn.execute(
            "INSERT INTO food_logs "
            "(username, eaten_at, eaten_day, meal_number, name, quantity_g, kcal, protein_g, "
            "carbs_g, fat_g, fiber_g, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(username, eaten_day, meal_number) DO UPDATE SET "
            "eaten_at = excluded.eaten_at, name = excluded.name, quantity_g = excluded.quantity_g, "
            "kcal = excluded.kcal, protein_g = excluded.protein_g, carbs_g = excluded.carbs_g, "
            "fat_g = excluded.fat_g, fiber_g = excluded.fiber_g, source = excluded.source",
            (
                username,
                when.isoformat(),
                day,
                meal_number,
                name,
                quantity_g,
                kcal,
                protein_g,
                carbs_g,
                fat_g,
                fiber_g,
                source,
            ),
        )
        self._conn.commit()
        return self._food_log(username, day, meal_number)

    def delete_food_log(self, username: str, meal_number: int, *, day: date | None = None) -> bool:
        """Remove a meal by its number (today by default). Returns whether a row went."""
        cur = self._conn.execute(
            "DELETE FROM food_logs WHERE username = ? AND eaten_day = ? AND meal_number = ?",
            (username, (day or date.today()).isoformat(), meal_number),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def _food_log(self, username: str, day: str, meal_number: int) -> FoodLog:
        row = self._conn.execute(
            "SELECT id, eaten_at, meal_number, name, quantity_g, kcal, protein_g, carbs_g, "
            "fat_g, fiber_g, source FROM food_logs "
            "WHERE username = ? AND eaten_day = ? AND meal_number = ?",
            (username, day, meal_number),
        ).fetchone()
        return self._to_food_log(row)

    def _to_food_log(self, r: sqlite3.Row) -> FoodLog:
        return FoodLog(
            id=r["id"],
            eaten_at=datetime.fromisoformat(r["eaten_at"]),
            meal_number=r["meal_number"],
            name=r["name"],
            quantity_g=r["quantity_g"],
            kcal=r["kcal"],
            protein_g=r["protein_g"],
            carbs_g=r["carbs_g"],
            fat_g=r["fat_g"],
            fiber_g=r["fiber_g"],
            source=r["source"],
        )

    def recent_food_logs(self, username: str, limit: int = 20) -> list[FoodLog]:
        rows = self._conn.execute(
            "SELECT id, eaten_at, meal_number, name, quantity_g, kcal, protein_g, carbs_g, "
            "fat_g, fiber_g, source FROM food_logs "
            "WHERE username = ? ORDER BY eaten_at DESC LIMIT ?",
            (username, limit),
        ).fetchall()
        return [self._to_food_log(r) for r in rows]

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

    def get_goals(self, username: str) -> Goals | None:
        row = self._conn.execute(
            "SELECT goal_mode, calorie_target_kcal, protein_target_g, fiber_target_g "
            "FROM goals WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return Goals(
            goal_mode=row["goal_mode"],
            calorie_target_kcal=row["calorie_target_kcal"],
            protein_target_g=row["protein_target_g"],
            fiber_target_g=row["fiber_target_g"],
        )

    def save_goals(self, username: str, goals: Goals) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO goals "
            "(username, goal_mode, calorie_target_kcal, protein_target_g, fiber_target_g) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                username,
                goals.goal_mode,
                goals.calorie_target_kcal,
                goals.protein_target_g,
                goals.fiber_target_g,
            ),
        )
        self._conn.commit()

    def day_food_logs(self, username: str, day: date) -> list[FoodLog]:
        """The day's meals in meal-number order, each carrying its number so a
        caller can pick which one to overwrite or delete."""
        rows = self._conn.execute(
            "SELECT id, eaten_at, meal_number, name, quantity_g, kcal, protein_g, carbs_g, "
            "fat_g, fiber_g, source FROM food_logs WHERE username = ? AND eaten_day = ? "
            "ORDER BY meal_number, eaten_at",
            (username, day.isoformat()),
        ).fetchall()
        return [self._to_food_log(r) for r in rows]

    def day_totals(self, username: str, day: date) -> DayTotals:
        # eaten_day is the single source of truth for which day a row belongs to
        # (delete_food and the meal-number index key on it too).
        row = self._conn.execute(
            "SELECT "
            "  COALESCE(SUM(kcal), 0)      AS kcal, "
            "  COALESCE(SUM(protein_g), 0) AS protein_g, "
            "  COALESCE(SUM(carbs_g), 0)   AS carbs_g, "
            "  COALESCE(SUM(fat_g), 0)     AS fat_g, "
            "  COALESCE(SUM(fiber_g), 0)   AS fiber_g, "
            "  COUNT(*)                    AS item_count "
            "FROM food_logs WHERE username = ? AND eaten_day = ?",
            (username, day.isoformat()),
        ).fetchone()
        return DayTotals(
            day=day,
            kcal=row["kcal"],
            protein_g=row["protein_g"],
            carbs_g=row["carbs_g"],
            fat_g=row["fat_g"],
            fiber_g=row["fiber_g"],
            item_count=row["item_count"],
        )
