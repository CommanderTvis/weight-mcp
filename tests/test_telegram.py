from datetime import date, datetime

import httpx
import pytest

from weight_mcp.models import FoodLog, Progress, WeightEntry
from weight_mcp.telegram import TelegramReporter, format_daily_report


def _meal(number: int, name: str, kcal: float, protein: float) -> FoodLog:
    return FoodLog(
        id=number,
        eaten_at=datetime(2026, 8, 2, 12, 0),
        meal_number=number,
        name=name,
        quantity_g=None,
        kcal=kcal,
        protein_g=protein,
        carbs_g=None,
        fat_g=None,
        source="manual",
    )


def _progress(**overrides: object) -> Progress:
    defaults: dict[str, object] = {
        "day": date(2026, 8, 2),
        "goal_mode": "floor",
        "kcal": 1900.0,
        "kcal_target": 2600,
        "protein_g": 120.0,
        "protein_target_g": 150,
    }
    return Progress(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_format_daily_report_full() -> None:
    text = format_daily_report(
        "admin",
        [_meal(1, "Skyr", 190, 33), _meal(2, "Burger", 650, 32)],
        _progress(fiber_g=12.0, fiber_target_g=30),
        WeightEntry(id=1, recorded_at=datetime(2026, 8, 2, 8, 0), weight_kg=82.44),
    )
    assert text == (
        "Daily report for admin — 2026-08-02\n"
        "\n"
        "1. Skyr — 190 kcal, 33 g protein\n"
        "2. Burger — 650 kcal, 32 g protein\n"
        "\n"
        "Total: 1900/2600 kcal (eat at least), 120/150 g protein, 12/30 g fiber\n"
        "Weight: 82.4 kg (2026-08-02)"
    )


def test_format_daily_report_empty_day_ceiling_no_weight() -> None:
    text = format_daily_report("alice", [], _progress(goal_mode="ceiling", kcal=0.0), None)
    assert "No meals logged." in text
    assert "(stay under)" in text
    assert "Weight:" not in text
    assert "fiber" not in text


def _reporter_with(handler: httpx.MockTransport) -> TelegramReporter:
    reporter = TelegramReporter("123:abc", "-1000042")
    reporter._client = httpx.AsyncClient(transport=handler)
    return reporter


async def test_send_posts_to_bot_api() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    reporter = _reporter_with(httpx.MockTransport(handler))
    await reporter.send("hello")
    await reporter.aclose()
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert b'"chat_id":"-1000042"' in seen["body"]  # type: ignore[operator]
    assert b'"text":"hello"' in seen["body"]  # type: ignore[operator]


async def test_send_raises_with_telegram_description() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"})

    reporter = _reporter_with(httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="bot was blocked"):
        await reporter.send("hello")
    await reporter.aclose()


async def test_send_unconfigured_refuses() -> None:
    reporter = TelegramReporter(None, None)
    assert not reporter.configured
    with pytest.raises(ValueError, match="not configured"):
        await reporter.send("hello")
    await reporter.aclose()
