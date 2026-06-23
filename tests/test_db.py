import sqlite3
from datetime import date, datetime
from pathlib import Path

from weight_mcp.db import Database
from weight_mcp.migrations import MIGRATIONS, migrate
from weight_mcp.models import Goals


def test_meal_numbers_autoincrement_then_overwrite(db: Database) -> None:
    first = db.add_food_log(name="oats", kcal=300, protein_g=10)
    second = db.add_food_log(name="eggs", kcal=200, protein_g=18)
    assert (first.meal_number, second.meal_number) == (1, 2)

    # Re-logging meal 1 overwrites it instead of adding a third row (the edit case).
    edited = db.add_food_log(name="oats (bigger)", kcal=450, protein_g=15, meal_number=1)
    assert edited.meal_number == 1
    logs = db.recent_food_logs()
    assert len(logs) == 2
    assert {log.meal_number for log in logs} == {1, 2}
    assert next(log for log in logs if log.meal_number == 1).kcal == 450


def test_delete_food_log(db: Database) -> None:
    db.add_food_log(name="oats", kcal=300, protein_g=10)
    assert db.delete_food_log(1) is True
    assert db.recent_food_logs() == []
    assert db.delete_food_log(1) is False  # already gone


def test_migration_preserves_pre_numbering_rows(tmp_path: Path) -> None:
    # A DB created before meal numbers existed (user_version 0, original schema).
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE food_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, eaten_at TEXT NOT NULL, "
        "name TEXT NOT NULL, quantity_g REAL, kcal REAL NOT NULL, protein_g REAL NOT NULL, "
        "carbs_g REAL, fat_g REAL, source TEXT);"
    )
    conn.execute(
        "INSERT INTO food_logs (eaten_at, name, kcal, protein_g) VALUES (?, ?, ?, ?)",
        ("2026-06-20T08:00:00", "oats", 300, 10),
    )
    conn.commit()

    migrate(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    row = conn.execute("SELECT name, eaten_day, meal_number FROM food_logs").fetchone()
    assert row["name"] == "oats"  # real data intact
    assert row["eaten_day"] == "2026-06-20"  # day backfilled
    assert row["meal_number"] is None  # historical row left unnumbered
    migrate(conn)  # idempotent: re-running is a no-op
    assert conn.execute("SELECT COUNT(*) AS n FROM food_logs").fetchone()["n"] == 1
    conn.close()


def test_weight_series_is_chronological(db: Database) -> None:
    db.add_weight(80.0, recorded_at=datetime(2026, 1, 2, 8, 0))
    db.add_weight(79.5, recorded_at=datetime(2026, 1, 1, 8, 0))
    series = db.weight_series()
    assert [e.weight_kg for e in series] == [79.5, 80.0]


def test_day_totals_sums_only_that_day(db: Database) -> None:
    db.add_food_log(name="oats", kcal=300, protein_g=10, eaten_at=datetime(2026, 1, 1, 9, 0))
    db.add_food_log(name="eggs", kcal=200, protein_g=18, eaten_at=datetime(2026, 1, 1, 12, 0))
    db.add_food_log(name="late", kcal=999, protein_g=99, eaten_at=datetime(2026, 1, 2, 9, 0))
    totals = db.day_totals(date(2026, 1, 1))
    assert totals.kcal == 500
    assert totals.protein_g == 28
    assert totals.item_count == 2


def test_empty_day_totals_are_zero(db: Database) -> None:
    totals = db.day_totals(date(2026, 1, 1))
    assert totals.kcal == 0
    assert totals.item_count == 0


def test_goals_roundtrip(db: Database) -> None:
    assert db.get_goals() is None  # unset until the user sets them
    db.save_goals(Goals(goal_mode="floor", calorie_target_kcal=2000, protein_target_g=107))
    stored = db.get_goals()
    assert stored is not None
    assert stored.protein_target_g == 107
    db.save_goals(Goals(goal_mode="ceiling", calorie_target_kcal=1800, protein_target_g=120))
    updated = db.get_goals()
    assert updated is not None
    assert updated.goal_mode == "ceiling"
    assert updated.calorie_target_kcal == 1800


def test_oauth_client_roundtrip(db: Database) -> None:
    db.add_oauth_client("abc", '{"client_id": "abc"}')
    assert db.get_oauth_client("abc") == '{"client_id": "abc"}'
    assert db.get_oauth_client("missing") is None
