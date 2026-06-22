"""Assembles the MCP server, the OAuth gate, and the dashboard into one ASGI app.

claude.ai connects to ``/`` (Streamable HTTP at the origin root). The MCP SDK
mounts the OAuth metadata, ``/authorize``, ``/token``, ``/register`` and the
401/``WWW-Authenticate`` handling for us; we add the password form at ``/login``
and the dashboard UI.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import GoalMode, Settings
from .db import Database
from .models import DEFAULT_GOALS, Goals, NutritionFacts, Progress
from .nutrition import NutritionLookup
from .oauth import DASHBOARD_COOKIE_TTL, SCOPE, PasswordOAuthProvider
from .ui import render_dashboard
from .web import login_page

DASHBOARD_URI = "ui://weight-mcp/dashboard"
UI_MIME = "text/html;profile=mcp-app"
LOGIN_PATH = "/login"
DASHBOARD_PATH = "/dashboard"
DASHBOARD_COOKIE = "wm_dash"

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
    # MCP is served at the origin root so the bare URL the user pastes into
    # claude.ai *is* the MCP endpoint, on the same origin as the OAuth routes.
    # Normalize through AnyHttpUrl so the token audience matches the trailing
    # slash the protected-resource metadata advertises for a root resource.
    resource_url = str(AnyHttpUrl(settings.issuer))
    provider = PasswordOAuthProvider(
        password=settings.password,
        resource_url=resource_url,
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
        instructions=(
            "Personal calorie & protein counter for one user. When the user opens "
            "or starts weight-mcp, greets you, or asks how they're doing, call "
            "`show_dashboard` first so they immediately see their weight graph, "
            "recent meals, and today's progress. Then help them log meals "
            "(`log_food`, `lookup_nutrition`), record weight (`record_weight`), "
            "and adjust targets (`set_goals`)."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(resource_url),
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
        goals = db.get_goals() or DEFAULT_GOALS
        return Progress(
            day=today,
            goal_mode=goals.goal_mode,
            kcal=totals.kcal,
            kcal_target=goals.calorie_target_kcal,
            protein_g=totals.protein_g,
            protein_target_g=goals.protein_target_g,
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
        goals = db.get_goals() or DEFAULT_GOALS
        goal_desc = (
            f"to eat AT LEAST {goals.calorie_target_kcal} kcal and "
            f"{goals.protein_target_g} g protein per day"
            if goals.goal_mode == "floor"
            else f"to stay UNDER {goals.calorie_target_kcal} kcal per day while "
            f"getting at least {goals.protein_target_g} g protein"
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

    @mcp.tool(
        title="Daily progress",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def daily_progress() -> Progress:
        """Today's calorie and protein intake against the configured goal."""
        return current_progress()

    @mcp.tool(title="Set goals")
    def set_goals(
        calorie_target_kcal: int | None = None,
        protein_target_g: int | None = None,
        goal_mode: GoalMode | None = None,
    ) -> Goals:
        """Update the daily targets. Only the arguments you pass are changed;
        goal_mode is "floor" (eat at least) or "ceiling" (stay under)."""
        current = db.get_goals() or DEFAULT_GOALS
        goals = current.model_copy(
            update={
                k: v
                for k, v in {
                    "calorie_target_kcal": calorie_target_kcal,
                    "protein_target_g": protein_target_g,
                    "goal_mode": goal_mode,
                }.items()
                if v is not None
            }
        )
        db.save_goals(goals)
        return goals

    @mcp.tool(
        title="Open weight-mcp dashboard",
        # Links the tool to its MCP Apps UI resource. The nested key is the
        # current spec; the flat alias is kept for host back-compat. Hosts that
        # render MCP Apps fetch DASHBOARD_URI; everyone else gets the text below,
        # which always includes a working web link.
        meta={
            "ui": {"resourceUri": DASHBOARD_URI, "visibility": ["model", "app"]},
            "ui/resourceUri": DASHBOARD_URI,
        },
        annotations=ToolAnnotations(
            title="Open weight-mcp dashboard",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def show_dashboard() -> str:
        """Open the weight-mcp dashboard: weight graph, recently eaten, and today's
        calorie/protein progress. This is the entry point — call it when the user
        starts or opens weight-mcp (e.g. "start weight mcp", "open weight", "show my
        dashboard"), at the beginning of a session, or whenever they ask how they're
        doing. Safe and read-only, so call it proactively without asking first.

        Returns a link to the dashboard; ALWAYS show the user that link verbatim so
        they can open it, since the inline panel may not render in every client. The
        page asks for the password once, then remembers the browser."""
        p = current_progress()
        link = f"{settings.issuer}{DASHBOARD_PATH}"
        return (
            f"Open your dashboard: {link}\n\n"
            f"Today: {p.kcal:.0f}/{p.kcal_target} kcal, "
            f"{p.protein_g:.0f}/{p.protein_target_g} g protein."
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
            if not provider.txn_valid(txn):
                return HTMLResponse(
                    login_page(
                        LOGIN_PATH, txn=txn, error="This link expired — reconnect from Claude."
                    ),
                    status_code=400,
                )
            return HTMLResponse(login_page(LOGIN_PATH, txn=txn))

        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        if not provider.txn_valid(txn):
            return HTMLResponse(
                login_page(LOGIN_PATH, txn=txn, error="This link expired — reconnect from Claude."),
                status_code=400,
            )
        if not provider.password_ok(password):
            return HTMLResponse(
                login_page(LOGIN_PATH, txn=txn, error="Incorrect password."), status_code=401
            )
        redirect = provider.complete_login(txn)
        if redirect is None:
            return HTMLResponse(
                login_page(LOGIN_PATH, txn=txn, error="This link expired — reconnect from Claude."),
                status_code=400,
            )
        return RedirectResponse(redirect, status_code=302)

    # --- dashboard web page (fallback link target) --------------------------

    @mcp.custom_route(DASHBOARD_PATH, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
    async def dashboard(request: Request) -> Response:
        # A stable, tokenless URL the model can reproduce verbatim. Access is
        # gated by a password-backed cookie set on first visit, not a URL token.
        if provider.dashboard_cookie_valid(request.cookies.get(DASHBOARD_COOKIE, "")):
            return HTMLResponse(dashboard_html())

        subtitle = "Enter your password to view your dashboard."
        if request.method == "POST":
            form = await request.form()
            if provider.password_ok(str(form.get("password", ""))):
                response: Response = RedirectResponse(DASHBOARD_PATH, status_code=302)
                response.set_cookie(
                    DASHBOARD_COOKIE,
                    provider.dashboard_cookie(),
                    max_age=DASHBOARD_COOKIE_TTL,
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    path=DASHBOARD_PATH,
                )
                return response
            return HTMLResponse(
                login_page(DASHBOARD_PATH, subtitle=subtitle, error="Incorrect password."),
                status_code=401,
            )
        return HTMLResponse(login_page(DASHBOARD_PATH, subtitle=subtitle))

    return mcp.streamable_http_app()
