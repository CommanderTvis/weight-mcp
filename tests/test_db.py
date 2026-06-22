from datetime import date, datetime

from weight_mcp.db import Database
from weight_mcp.models import Goals


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
