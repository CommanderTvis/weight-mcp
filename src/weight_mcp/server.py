"""Assembles the MCP server, the OAuth gate, and the dashboard into one ASGI app.

claude.ai connects to ``/mcp`` (Streamable HTTP). The MCP SDK mounts the OAuth
metadata, ``/authorize``, ``/token``, ``/register`` and the 401/``WWW-Authenticate``
handling for us; we add the password form at ``/login`` and the dashboard UI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import EmbeddedResource, TextResourceContents
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings
from .db import Database
from .models import NutritionFacts, Progress
from .nutrition import NutritionLookup
from .oauth import SCOPE, PasswordOAuthProvider
from .ui import render_dashboard
from .web import login_page

DASHBOARD_URI = "ui://weight-mcp/dashboard"
UI_MIME = "text/html;profile=mcp-app"
LOGIN_PATH = "/login"

PROMPT_TEXT = """\
You are a calorie and protein counter. The user will tell you what they ate \
(in text or as photos) and report their body weight.

For each food the user reports:
1. Identify the item and estimate the amount in grams.
2. Use `lookup_nutrition` to find calories and protein for it (try a barcode if \
the user gives one, otherwise search by name). Pick the best match and scale it \
to the amount eaten.
3. Call `log_food` with the resulting kcal and protein.

Use `record_weight` whenever the user reports a weight. Call `daily_progress` to \
check intake against the goal, and `show_dashboard` to display the weight graph \
and recent meals (for example at the end of the day).

The user's goal is {goal_desc}. Be encouraging and concrete about what's left to \
hit it today."""


def create_app(settings: Settings) -> Starlette:
    db = Database(settings.database_path)
    nutrition = NutritionLookup(settings)
    mcp_url = f"{settings.issuer}/mcp"
    provider = PasswordOAuthProvider(
        password=settings.password,
        resource_url=mcp_url,
        login_path=LOGIN_PATH,
        db=db,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await nutrition.aclose()
            db.close()

    mcp = FastMCP(
        "weight-mcp",
        instructions="Personal calorie & protein counter. Log meals, track weight, "
        "and view a dashboard — all in chat.",
        host=settings.host,
        port=settings.port,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(mcp_url),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[SCOPE],
                default_scopes=[SCOPE],
            ),
            required_scopes=[SCOPE],
        ),
        lifespan=lifespan,
    )

    def current_progress() -> Progress:
        today = date.today()
        totals = db.day_totals(today)
        return Progress(
            day=today,
            goal_mode=settings.goal_mode,
            kcal=totals.kcal,
            kcal_target=settings.calorie_target_kcal,
            protein_g=totals.protein_g,
            protein_target_g=settings.protein_target_g,
        )

    def dashboard_html() -> str:
        return render_dashboard(
            db.weight_series(limit=180),
            db.recent_food_logs(limit=20),
            current_progress(),
        )

    # --- prompt -------------------------------------------------------------

    @mcp.prompt(title="Calorie & protein counter")
    def counter() -> str:
        goal_desc = (
            f"to eat AT LEAST {settings.calorie_target_kcal} kcal and "
            f"{settings.protein_target_g} g protein per day"
            if settings.goal_mode == "floor"
            else f"to stay UNDER {settings.calorie_target_kcal} kcal per day while "
            f"getting at least {settings.protein_target_g} g protein"
        )
        return PROMPT_TEXT.format(goal_desc=goal_desc)

    # --- tools --------------------------------------------------------------

    @mcp.tool(title="Record weight")
    def record_weight(weight_kg: float) -> str:
        """Store a body-weight measurement (in kilograms)."""
        entry = db.add_weight(weight_kg)
        return f"Recorded {entry.weight_kg:.1f} kg at {entry.recorded_at:%Y-%m-%d %H:%M}."

    @mcp.tool(title="Log food")
    def log_food(
        name: str,
        kcal: float,
        protein_g: float,
        quantity_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
    ) -> str:
        """Log one eaten item with its calories and protein (already scaled to the
        amount eaten)."""
        db.add_food_log(
            name=name,
            kcal=kcal,
            protein_g=protein_g,
            quantity_g=quantity_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            source="manual",
        )
        p = current_progress()
        return (
            f"Logged {name}: {kcal:.0f} kcal, {protein_g:.0f} g protein. "
            f"Today: {p.kcal:.0f}/{p.kcal_target} kcal, "
            f"{p.protein_g:.0f}/{p.protein_target_g} g protein."
        )

    @mcp.tool(title="Look up nutrition")
    async def lookup_nutrition(query: str = "", barcode: str | None = None) -> list[NutritionFacts]:
        """Search public nutrition databases for calories and protein (per 100 g).
        Provide a barcode for an exact product, or a text query to search."""
        if barcode:
            hit = await nutrition.by_barcode(barcode)
            return [hit] if hit else []
        return await nutrition.search(query)

    @mcp.tool(title="Daily progress")
    def daily_progress() -> Progress:
        """Today's calorie and protein intake against the configured goal."""
        return current_progress()

    @mcp.tool(title="Show dashboard", meta={"ui": {"resourceUri": DASHBOARD_URI}})
    def show_dashboard() -> EmbeddedResource:
        """Render the interactive dashboard: weight graph and recently eaten."""
        return EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                uri=AnyUrl(DASHBOARD_URI),
                mimeType=UI_MIME,
                text=dashboard_html(),
            ),
        )

    # --- UI resource (host fetches it via the tool's _meta.ui.resourceUri) --

    @mcp.resource(DASHBOARD_URI, mime_type=UI_MIME)
    def dashboard_resource() -> str:
        return dashboard_html()

    # --- OAuth password form ------------------------------------------------

    @mcp.custom_route(LOGIN_PATH, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
    async def login(request: Request) -> Response:
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            if not provider.pending_exists(txn):
                return HTMLResponse(
                    login_page(LOGIN_PATH, txn, error="This link expired — reconnect from Claude."),
                    status_code=400,
                )
            return HTMLResponse(login_page(LOGIN_PATH, txn))

        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        if not provider.pending_exists(txn):
            return HTMLResponse(
                login_page(LOGIN_PATH, txn, error="This link expired — reconnect from Claude."),
                status_code=400,
            )
        if not provider.password_ok(password):
            return HTMLResponse(
                login_page(LOGIN_PATH, txn, error="Incorrect password."), status_code=401
            )
        redirect = provider.complete_login(txn)
        if redirect is None:
            return HTMLResponse(
                login_page(LOGIN_PATH, txn, error="This link expired — reconnect from Claude."),
                status_code=400,
            )
        return RedirectResponse(redirect, status_code=302)

    return mcp.streamable_http_app()
