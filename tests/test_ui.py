from __future__ import annotations

from datetime import date, datetime

from weight_mcp.models import FoodLog, Progress, WeightEntry
from weight_mcp.ui import render_dashboard


def _progress(mode: str = "floor") -> Progress:
    return Progress(
        day=date(2026, 1, 1),
        goal_mode=mode,
        kcal=1800,
        kcal_target=2600,
        protein_g=120,
        protein_target_g=150,
    )


def test_render_includes_data_and_escapes_names() -> None:
    weights = [
        WeightEntry(id=1, recorded_at=datetime(2026, 1, 1), weight_kg=80.0),
        WeightEntry(id=2, recorded_at=datetime(2026, 1, 2), weight_kg=79.4),
    ]
    logs = [
        FoodLog(
            id=1,
            eaten_at=datetime(2026, 1, 1, 12, 0),
            name="<b>Brötchen</b>",
            quantity_g=80,
            kcal=210,
            protein_g=7,
            carbs_g=None,
            fat_g=None,
            source="manual",
        )
    ]
    html = render_dashboard(weights, logs, _progress())
    assert "<polyline" in html  # chart drawn
    assert "2600 kcal" in html
    assert "&lt;b&gt;Br" in html  # meal name HTML-escaped
    assert "<b>Br" not in html


def test_render_handles_no_data() -> None:
    html = render_dashboard([], [], _progress())
    assert "Not enough weight entries" in html
    assert "Nothing logged yet" in html
