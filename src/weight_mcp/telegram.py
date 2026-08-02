"""Daily-report delivery to an accountability partner via a Telegram bot.

Configured with a bot token and a chat ID from the environment. The chat ID may
point at a human, a group, or a channel: Telegram accepts a numeric ID for all
three (groups/channels are negative numbers) and ``@channelusername`` for public
channels/groups, so it is kept as a string. When either value is missing the
``send_daily_report`` tool is simply not registered.
"""

import httpx
from pydantic import BaseModel, ConfigDict

from .models import FoodLog, Progress, WeightEntry

_API_BASE = "https://api.telegram.org"


class TelegramResponse(BaseModel):
    """The envelope every Bot API method replies with."""

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    description: str | None = None


def format_daily_report(
    username: str,
    meals: list[FoodLog],
    progress: Progress,
    latest_weight: WeightEntry | None,
) -> str:
    """The plain-text message sent to the partner: the day's meals, totals
    against the targets, and the most recent weight."""
    lines = [f"Daily report for {username} — {progress.day:%Y-%m-%d}", ""]
    if meals:
        lines.extend(
            f"{m.meal_number}. {m.name} — {m.kcal:.0f} kcal, {m.protein_g:.0f} g protein"
            for m in meals
        )
    else:
        lines.append("No meals logged.")
    goal = "eat at least" if progress.goal_mode == "floor" else "stay under"
    totals = (
        f"Total: {progress.kcal:.0f}/{progress.kcal_target} kcal ({goal}), "
        f"{progress.protein_g:.0f}/{progress.protein_target_g} g protein"
    )
    if progress.fiber_target_g is not None:
        totals += f", {progress.fiber_g:.0f}/{progress.fiber_target_g} g fiber"
    lines.extend(["", totals])
    if latest_weight is not None:
        lines.append(
            f"Weight: {latest_weight.weight_kg:.1f} kg ({latest_weight.recorded_at:%Y-%m-%d})"
        )
    return "\n".join(lines)


class TelegramReporter:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, text: str) -> None:
        """Deliver one plain-text message; raises ValueError with Telegram's
        explanation on failure so the tool surfaces it to the model."""
        if not self.configured:
            raise ValueError(
                "Telegram reporting is not configured: set WEIGHT_MCP_TELEGRAM_BOT_TOKEN "
                "and WEIGHT_MCP_TELEGRAM_CHAT_ID."
            )
        resp = await self._client.post(
            f"{_API_BASE}/bot{self._bot_token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text},
        )
        try:
            parsed = TelegramResponse.model_validate(resp.json())
        except ValueError:
            parsed = TelegramResponse()
        if not parsed.ok:
            reason = parsed.description or f"HTTP {resp.status_code}"
            raise ValueError(f"Telegram rejected the report: {reason}")
