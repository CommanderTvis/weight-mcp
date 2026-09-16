from datetime import date, datetime

from weight_mcp.models import FoodLog, Progress, WeightEntry
from weight_mcp.ui import render_dashboard


def _progress(mode: str = "floor", fiber_target_g: int | None = None) -> Progress:
    return Progress(
        day=date(2026, 1, 1),
        goal_mode=mode,
        kcal=1800,
        kcal_target=2600,
        protein_g=120,
        protein_target_g=150,
        fiber_g=12,
        fiber_target_g=fiber_target_g,
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


def test_fiber_card_only_when_norm_is_set() -> None:
    assert "g fiber" not in render_dashboard([], [], _progress())
    html = render_dashboard([], [], _progress(fiber_target_g=30))
    assert "30 g fiber" in html


def test_render_handles_no_data() -> None:
    html = render_dashboard([], [], _progress())
    assert "Not enough weight entries" in html
    assert "Nothing logged yet" in html


def test_app_bridge_is_opt_in() -> None:
    # The MCP Apps bridge (connect() + auto-resize) is only embedded on request,
    # so the plain web page doesn't pull in the CDN script.
    assert "app-with-deps.js" not in render_dashboard([], [], _progress())
    embedded = render_dashboard([], [], _progress(), embed_app_bridge=True)
    assert "app-with-deps.js" in embedded
    assert ".connect()" in embedded


def test_panel_rereads_itself_once_the_bridge_connects() -> None:
    # The stat cards are baked in server-side, so a host serving a cached copy of
    # the resource would show pre-delete numbers forever without this re-read.
    embedded = render_dashboard([], [], _progress(), embed_app_bridge=True)
    assert "window.__wmRefresh = async" in embedded
    assert "await window.__wmRefresh();" in embedded
    # Framed but not yet connected: don't fetch the host's own document.
    assert "window.self === window.top" in embedded


def test_recently_eaten_grouped_by_days() -> None:
    progress = Progress(
        day=date(2026, 1, 3),
        goal_mode="floor",
        kcal=1800,
        kcal_target=2600,
        protein_g=120,
        protein_target_g=150,
    )
    logs = [
        FoodLog(
            id=1,
            eaten_at=datetime(2026, 1, 3, 9, 30),
            meal_number=1,
            name="Oats",
            kcal=300,
            protein_g=10,
            quantity_g=80,
            carbs_g=None,
            fat_g=None,
            source="manual",
        ),
        FoodLog(
            id=2,
            eaten_at=datetime(2026, 1, 3, 13, 0),
            meal_number=2,
            name="Chicken salad",
            kcal=450,
            protein_g=40,
            quantity_g=300,
            carbs_g=None,
            fat_g=None,
            source="manual",
        ),
        FoodLog(
            id=3,
            eaten_at=datetime(2026, 1, 2, 20, 0),
            meal_number=2,
            name="Steak",
            kcal=600,
            protein_g=50,
            quantity_g=250,
            carbs_g=None,
            fat_g=None,
            source="manual",
        ),
        FoodLog(
            id=4,
            eaten_at=datetime(2026, 1, 1, 18, 30),
            meal_number=1,
            name="Salmon",
            kcal=500,
            protein_g=45,
            quantity_g=200,
            carbs_g=None,
            fat_g=None,
            source="manual",
        ),
    ]
    html = render_dashboard([], logs, progress)

    # Check day titles
    assert "Today — Saturday, Jan 3" in html
    assert "Yesterday — Friday, Jan 2" in html
    assert "Thursday, Jan 1" in html

    # Check meals are ordered within their day (Chicken salad at 13:00 before Oats at 09:30)
    idx_chicken = html.index("Chicken salad")
    idx_oats = html.index("Oats")
    idx_steak = html.index("Steak")
    idx_salmon = html.index("Salmon")
    assert idx_chicken < idx_oats < idx_steak < idx_salmon

    # Check time format is HH:MM without redundant weekday
    assert "13:00</span></li>" in html
    assert "09:30</span></li>" in html
    assert "20:00</span></li>" in html
    assert "18:30</span></li>" in html
    assert "Sat 13:00" not in html

