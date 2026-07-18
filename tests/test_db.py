import sqlite3
from datetime import date, datetime
from pathlib import Path

from weight_mcp.db import Database, hash_password, verify_password
from weight_mcp.migrations import MIGRATIONS, migrate
from weight_mcp.models import Goals

USER = "admin"


def test_meal_numbers_autoincrement_then_overwrite(db: Database) -> None:
    first = db.add_food_log(USER, name="oats", kcal=300, protein_g=10)
    second = db.add_food_log(USER, name="eggs", kcal=200, protein_g=18)
    assert (first.meal_number, second.meal_number) == (1, 2)

    # Re-logging meal 1 overwrites it instead of adding a third row (the edit case).
    edited = db.add_food_log(USER, name="oats (bigger)", kcal=450, protein_g=15, meal_number=1)
    assert edited.meal_number == 1
    logs = db.recent_food_logs(USER)
    assert len(logs) == 2
    assert {log.meal_number for log in logs} == {1, 2}
    assert next(log for log in logs if log.meal_number == 1).kcal == 450


def test_delete_food_log(db: Database) -> None:
    db.add_food_log(USER, name="oats", kcal=300, protein_g=10)
    assert db.delete_food_log(USER, 1) is True
    assert db.recent_food_logs(USER) == []
    assert db.delete_food_log(USER, 1) is False  # already gone


def test_day_food_logs_lists_a_days_meals_in_number_order(db: Database) -> None:
    day = date(2026, 3, 4)
    other = datetime(2026, 3, 5, 9, 0)
    db.add_food_log(USER, name="oats", kcal=300, protein_g=10, eaten_at=datetime(2026, 3, 4, 8, 0))
    db.add_food_log(USER, name="eggs", kcal=200, protein_g=18, eaten_at=datetime(2026, 3, 4, 12, 0))
    db.add_food_log(USER, name="tomorrow", kcal=100, protein_g=1, eaten_at=other)

    meals = db.day_food_logs(USER, day)
    assert [(m.meal_number, m.name) for m in meals] == [(1, "oats"), (2, "eggs")]


def test_migration_numbers_pre_numbering_rows(tmp_path: Path) -> None:
    # A DB created before meal numbers existed (user_version 0, original schema).
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE food_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, eaten_at TEXT NOT NULL, "
        "name TEXT NOT NULL, quantity_g REAL, kcal REAL NOT NULL, protein_g REAL NOT NULL, "
        "carbs_g REAL, fat_g REAL, source TEXT);"
    )
    # Two rows on one day, one on another, out of eaten_at order.
    conn.executemany(
        "INSERT INTO food_logs (eaten_at, name, kcal, protein_g) VALUES (?, ?, ?, ?)",
        [
            ("2026-06-20T12:00:00", "lunch", 500, 20),
            ("2026-06-20T08:00:00", "oats", 300, 10),
            ("2026-06-21T08:00:00", "eggs", 200, 15),
        ],
    )
    conn.commit()

    migrate(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    rows = conn.execute(
        "SELECT name, eaten_day, meal_number, username FROM food_logs "
        "ORDER BY eaten_day, meal_number"
    ).fetchall()
    # Days backfilled, every historical row numbered per day (oldest first), and
    # all pre-multi-user data assigned to the admin account.
    assert [(r["name"], r["eaten_day"], r["meal_number"], r["username"]) for r in rows] == [
        ("oats", "2026-06-20", 1, "admin"),
        ("lunch", "2026-06-20", 2, "admin"),
        ("eggs", "2026-06-21", 1, "admin"),
    ]
    migrate(conn)  # idempotent: re-running is a no-op
    assert conn.execute("SELECT COUNT(*) AS n FROM food_logs").fetchone()["n"] == 3
    conn.close()


def test_migration_moves_global_goals_to_admin(tmp_path: Path) -> None:
    # A DB from before multi-user (migrations 1-3 applied) with goals set.
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for step in MIGRATIONS[:3]:
        step(conn)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO goals (id, goal_mode, calorie_target_kcal, protein_target_g) "
        "VALUES (1, 'ceiling', 1800, 120)"
    )
    conn.commit()

    migrate(conn)

    row = conn.execute("SELECT * FROM goals").fetchone()
    assert (row["username"], row["goal_mode"], row["calorie_target_kcal"]) == (
        "admin",
        "ceiling",
        1800,
    )
    conn.close()


def test_weight_series_is_chronological(db: Database) -> None:
    db.add_weight(USER, 80.0, recorded_at=datetime(2026, 1, 2, 8, 0))
    db.add_weight(USER, 79.5, recorded_at=datetime(2026, 1, 1, 8, 0))
    series = db.weight_series(USER)
    assert [e.weight_kg for e in series] == [79.5, 80.0]


def test_day_totals_sums_only_that_day(db: Database) -> None:
    db.add_food_log(USER, name="oats", kcal=300, protein_g=10, eaten_at=datetime(2026, 1, 1, 9, 0))
    db.add_food_log(USER, name="eggs", kcal=200, protein_g=18, eaten_at=datetime(2026, 1, 1, 12, 0))
    db.add_food_log(USER, name="late", kcal=999, protein_g=99, eaten_at=datetime(2026, 1, 2, 9, 0))
    totals = db.day_totals(USER, date(2026, 1, 1))
    assert totals.kcal == 500
    assert totals.protein_g == 28
    assert totals.item_count == 2


def test_empty_day_totals_are_zero(db: Database) -> None:
    totals = db.day_totals(USER, date(2026, 1, 1))
    assert totals.kcal == 0
    assert totals.item_count == 0


def test_goals_roundtrip(db: Database) -> None:
    assert db.get_goals(USER) is None  # unset until the user sets them
    db.save_goals(USER, Goals(goal_mode="floor", calorie_target_kcal=2000, protein_target_g=107))
    stored = db.get_goals(USER)
    assert stored is not None
    assert stored.protein_target_g == 107
    db.save_goals(USER, Goals(goal_mode="ceiling", calorie_target_kcal=1800, protein_target_g=120))
    updated = db.get_goals(USER)
    assert updated is not None
    assert updated.goal_mode == "ceiling"
    assert updated.calorie_target_kcal == 1800


def test_oauth_client_roundtrip(db: Database) -> None:
    db.add_oauth_client("abc", '{"client_id": "abc"}')
    assert db.get_oauth_client("abc") == '{"client_id": "abc"}'
    assert db.get_oauth_client("missing") is None


# --- multi-user -------------------------------------------------------------


def test_password_hash_roundtrip() -> None:
    stored = hash_password("hunter22")
    assert verify_password("hunter22", stored)
    assert not verify_password("hunter23", stored)
    assert not verify_password("hunter22", "not-a-hash")
    # Salted: hashing the same password twice yields different strings.
    assert stored != hash_password("hunter22")


def test_user_crud(db: Database) -> None:
    assert db.list_users() == []
    assert db.create_user("alice", hash_password("pw-alice-1")) is True
    assert db.create_user("alice", hash_password("other")) is False  # taken
    assert db.list_users() == ["alice"]
    assert db.get_user_password_hash("nobody") is None

    stored = db.get_user_password_hash("alice")
    assert stored is not None and verify_password("pw-alice-1", stored)

    assert db.set_user_password("alice", hash_password("pw-alice-2")) is True
    stored = db.get_user_password_hash("alice")
    assert stored is not None and verify_password("pw-alice-2", stored)
    assert db.set_user_password("nobody", hash_password("x")) is False

    assert db.delete_user("alice") is True
    assert db.delete_user("alice") is False
    assert db.list_users() == []


def test_data_is_scoped_per_user(db: Database) -> None:
    db.add_food_log("alice", name="oats", kcal=300, protein_g=10)
    db.add_food_log("bob", name="eggs", kcal=200, protein_g=18)
    db.add_weight("alice", 60.0)
    db.save_goals(
        "alice", Goals(goal_mode="ceiling", calorie_target_kcal=1500, protein_target_g=90)
    )

    # Meal numbers count per user: bob's first meal is also #1.
    assert [m.name for m in db.day_food_logs("alice", date.today())] == ["oats"]
    assert [(m.meal_number, m.name) for m in db.day_food_logs("bob", date.today())] == [(1, "eggs")]
    assert db.day_totals("alice", date.today()).kcal == 300
    assert db.day_totals("bob", date.today()).kcal == 200
    assert db.weight_series("bob") == []
    assert db.get_goals("bob") is None
    goals = db.get_goals("alice")
    assert goals is not None and goals.calorie_target_kcal == 1500

    # Deleting bob's meal #1 must not touch alice's meal #1.
    assert db.delete_food_log("bob", 1) is True
    assert [m.name for m in db.day_food_logs("alice", date.today())] == ["oats"]
