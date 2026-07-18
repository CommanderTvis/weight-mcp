"""Assembles the MCP server, the OAuth gate, and the dashboard into one ASGI app.

claude.ai connects to ``/`` (Streamable HTTP at the origin root). The MCP SDK
mounts the OAuth metadata, ``/authorize``, ``/token``, ``/register`` and the
401/``WWW-Authenticate`` handling for us; we add the login form at ``/login``
and the dashboard UI.

Multi-user: every request is authenticated as a username (the OAuth token's
``sub``), and every tool reads/writes only that user's rows. The admin account
(``admin``, password from ``.env``) additionally gets account-management tools
to register, deregister, and reset passwords of DB-backed non-admin users.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import GoalMode, Settings
from .db import Database, hash_password
from .models import DEFAULT_GOALS, FoodLog, Goals, NutritionFacts, Progress
from .nutrition import NutritionLookup
from .oauth import ADMIN_USERNAME, DASHBOARD_COOKIE_TTL, SCOPE, PasswordOAuthProvider
from .ui import APP_BRIDGE_ORIGIN, DASHBOARD_URI, render_dashboard
from .web import login_page

UI_MIME = "text/html;profile=mcp-app"
LOGIN_PATH = "/login"
DASHBOARD_PATH = "/dashboard"
DASHBOARD_COOKIE = "wm_dash"

# What may be registered as a username. ``admin`` is reserved for the .env account.
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,31}$")
MIN_PASSWORD_LEN = 8

PROMPT_TEXT = """\
You are a calorie and protein counter. The user will tell you what they ate \
(in text or as photos) and report their body weight.

For each food the user reports:
1. Identify the item and estimate the amount in grams.
2. Use `lookup_nutrition` to find calories and protein for it (try a barcode if \
the user gives one, otherwise search by name). Pick the best match and scale it \
to the amount eaten.
3. Call `log_food` with the resulting kcal and protein, numbering meals by the \
order the user reports them: the first food is meal 1, the next is 2, and so on.

If the user corrects a meal — or edits an earlier message so a food changes — \
re-log it with the SAME meal number to overwrite it (don't add a duplicate); use \
`delete_food` to remove one. Because an edited message re-runs from that point, \
keep numbering by conversation order so the corrected food keeps its number. If \
you don't know a meal's number (a past day, or a fresh conversation), call \
`list_meals` to see the day's meals and their numbers before editing or deleting.

If the user reports food eaten on a past day ("yesterday I had..."), pass that \
day as `day` (YYYY-MM-DD) to `log_food` (and to `delete_food` when removing); \
meal numbers count per day, so continue from that day's own meals.

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
        admin_password=settings.password,
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
            "Personal calorie & protein counter. Each connected account sees only "
            "its own data. When the user opens or starts weight-mcp, greets you, "
            "or asks how they're doing, call `show_dashboard` first so they "
            "immediately see their weight graph, recent meals, and today's "
            "progress. Then help them log meals (`log_food`, `lookup_nutrition`), "
            "record weight (`record_weight`), and adjust targets (`set_goals`). "
            "To correct or remove a meal, call `list_meals` to get its number, "
            "then `delete_food` (or re-log with that number to overwrite) — never "
            "guess the number. The admin account can additionally manage user "
            "accounts with `register_user`, `deregister_user`, and "
            "`update_user_password`."
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

    def current_username() -> str:
        """The account this MCP request is authenticated as (the token's sub)."""
        token = get_access_token()
        if token is None or not token.subject:
            raise ValueError("Not authenticated")
        return token.subject

    def require_admin() -> None:
        if current_username() != ADMIN_USERNAME:
            raise ValueError("Only the admin account can manage users.")

    def current_progress(username: str, day: date | None = None) -> Progress:
        day = day or date.today()
        totals = db.day_totals(username, day)
        goals = db.get_goals(username) or DEFAULT_GOALS
        return Progress(
            day=day,
            goal_mode=goals.goal_mode,
            kcal=totals.kcal,
            kcal_target=goals.calorie_target_kcal,
            protein_g=totals.protein_g,
            protein_target_g=goals.protein_target_g,
        )

    def dashboard_html(username: str, *, embed_app_bridge: bool = False) -> str:
        return render_dashboard(
            db.weight_series(username, limit=180),
            db.recent_food_logs(username, limit=20),
            current_progress(username),
            embed_app_bridge=embed_app_bridge,
        )

    # --- prompt -------------------------------------------------------------

    @mcp.prompt(title="Calorie & protein counter")
    def counter() -> str:
        goals = db.get_goals(current_username()) or DEFAULT_GOALS
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
        entry = db.add_weight(current_username(), weight_kg)
        return f"Recorded {entry.weight_kg:.1f} kg at {entry.recorded_at:%Y-%m-%d %H:%M}."

    @mcp.tool(title="Log food")
    def log_food(
        name: str,
        kcal: float,
        protein_g: float,
        meal_number: int | None = None,
        quantity_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
        day: date | None = None,
    ) -> str:
        """Log one eaten item with its calories and protein (already scaled to the
        amount eaten). Number meals by their order in the conversation: the first
        food reported is meal_number 1, the next 2, and so on — always pass it. To
        revise a meal (the user corrects it, or edits an earlier message), call this
        again with that same meal_number to OVERWRITE it instead of duplicating.
        Pass `day` (YYYY-MM-DD) to log for a past day, e.g. yesterday; meal numbers
        count per day, so start from that day's existing meals."""
        username = current_username()
        eaten_at = None
        if day is not None and day != date.today():
            eaten_at = datetime.combine(day, datetime.now().time())
        entry = db.add_food_log(
            username,
            name=name,
            kcal=kcal,
            protein_g=protein_g,
            quantity_g=quantity_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            source="manual",
            eaten_at=eaten_at,
            meal_number=meal_number,
        )
        p = current_progress(username, day)
        label = "Today" if p.day == date.today() else f"{p.day:%Y-%m-%d}"
        return (
            f"Meal #{entry.meal_number}: {name} — {kcal:.0f} kcal, {protein_g:.0f} g protein. "
            f"{label}: {p.kcal:.0f}/{p.kcal_target} kcal, "
            f"{p.protein_g:.0f}/{p.protein_target_g} g protein."
        )

    @mcp.tool(
        title="List meals",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def list_meals(day: date | None = None) -> list[FoodLog]:
        """List the meals logged on a day (today by default), each with its
        meal_number — call this to find the number before overwriting a meal with
        `log_food` or removing one with `delete_food`. Pass `day` (YYYY-MM-DD) for
        a past day."""
        return db.day_food_logs(current_username(), day or date.today())

    @mcp.tool(title="Delete meal")
    def delete_food(meal_number: int, day: date | None = None) -> str:
        """Remove a logged meal by its meal_number. Defaults to today; pass `day`
        (YYYY-MM-DD) to delete from a past day. If you don't know the number, call
        `list_meals` first — don't guess."""
        username = current_username()
        d = day or date.today()
        if not db.delete_food_log(username, meal_number, day=d):
            meals = db.day_food_logs(username, d)
            if not meals:
                return f"No meals logged on {d:%Y-%m-%d}, so nothing to delete."
            listing = "; ".join(f"#{m.meal_number} {m.name}" for m in meals)
            return (
                f"No meal #{meal_number} on {d:%Y-%m-%d}. That day's meals are: "
                f"{listing}. Retry with the right number."
            )
        p = current_progress(username, day)
        label = "Today" if p.day == date.today() else f"{p.day:%Y-%m-%d}"
        return (
            f"Removed meal #{meal_number}. {label}: {p.kcal:.0f}/{p.kcal_target} kcal, "
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
        return current_progress(current_username())

    @mcp.tool(title="Set goals")
    def set_goals(
        calorie_target_kcal: int | None = None,
        protein_target_g: int | None = None,
        goal_mode: GoalMode | None = None,
    ) -> Goals:
        """Update the daily targets. Only the arguments you pass are changed;
        goal_mode is "floor" (eat at least) or "ceiling" (stay under)."""
        username = current_username()
        current = db.get_goals(username) or DEFAULT_GOALS
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
        db.save_goals(username, goals)
        return goals

    # --- account management (admin only) ------------------------------------

    @mcp.tool(title="Register user")
    def register_user(username: str, password: str) -> str:
        """Admin only: register a new non-admin account. The new user connects
        this server to their own claude.ai with that username and password.
        Usernames are 1-32 chars: letters, digits, ``.``, ``_``, ``-``."""
        require_admin()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError(
                "Invalid username: use 1-32 letters, digits, '.', '_' or '-', "
                "starting with a letter or digit."
            )
        if username == ADMIN_USERNAME:
            raise ValueError(f"'{ADMIN_USERNAME}' is reserved for the admin account.")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        if not db.create_user(username, hash_password(password)):
            raise ValueError(f"User '{username}' already exists.")
        return (
            f"Registered user '{username}'. They can now connect this server in "
            f"claude.ai and sign in with that username and password."
        )

    @mcp.tool(
        title="Deregister user",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    def deregister_user(username: str) -> str:
        """Admin only: deregister a non-admin account. Their tokens stop working
        immediately. Their logged data is kept — re-registering the same username
        later reattaches it."""
        require_admin()
        if username == ADMIN_USERNAME:
            raise ValueError("The admin account lives in .env and cannot be deregistered.")
        if not db.delete_user(username):
            known = ", ".join(db.list_users()) or "none"
            raise ValueError(f"No user '{username}'. Registered users: {known}.")
        return f"Deregistered user '{username}'. Their access is revoked; their data is kept."

    @mcp.tool(title="Update user password")
    def update_user_password(username: str, password: str) -> str:
        """Admin only: set a new password for a non-admin account. All of that
        user's existing tokens are invalidated; they must reconnect with the new
        password. (The admin password itself is changed in .env, not here.)"""
        require_admin()
        if username == ADMIN_USERNAME:
            raise ValueError("The admin password is set via .env (WEIGHT_MCP_PASSWORD).")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
        if not db.set_user_password(username, hash_password(password)):
            known = ", ".join(db.list_users()) or "none"
            raise ValueError(f"No user '{username}'. Registered users: {known}.")
        return f"Password updated for '{username}'. They must reconnect with the new password."

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

        The dashboard renders inline as an interactive panel — do NOT print a link
        or repeat its contents in text. The short text returned here is only a
        fallback summary for clients that can't render the panel."""
        p = current_progress(current_username())
        return (
            f"Today: {p.kcal:.0f}/{p.kcal_target} kcal, "
            f"{p.protein_g:.0f}/{p.protein_target_g} g protein."
        )

    # --- UI resource (host fetches it via the tool's _meta.ui.resourceUri) --
    # csp.resourceDomains must allow the app-bridge CDN, or the host's CSP blocks
    # the script and the iframe never connects/resizes.

    @mcp.resource(
        DASHBOARD_URI,
        mime_type=UI_MIME,
        meta={"ui": {"csp": {"resourceDomains": [APP_BRIDGE_ORIGIN]}}},
    )
    def dashboard_resource() -> str:
        return dashboard_html(current_username(), embed_app_bridge=True)

    # --- OAuth login form ----------------------------------------------------

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
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if not provider.txn_valid(txn):
            return HTMLResponse(
                login_page(LOGIN_PATH, txn=txn, error="This link expired — reconnect from Claude."),
                status_code=400,
            )
        if not provider.verify_login(username, password):
            return HTMLResponse(
                login_page(LOGIN_PATH, txn=txn, error="Incorrect username or password."),
                status_code=401,
            )
        redirect = provider.complete_login(txn, username)
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
        # The cookie names the account, so each user sees their own dashboard.
        cookie_user = provider.dashboard_cookie_user(request.cookies.get(DASHBOARD_COOKIE, ""))
        if cookie_user is not None:
            return HTMLResponse(dashboard_html(cookie_user))

        subtitle = "Sign in to view your dashboard."
        if request.method == "POST":
            form = await request.form()
            username = str(form.get("username", ""))
            if provider.verify_login(username, str(form.get("password", ""))):
                response: Response = RedirectResponse(DASHBOARD_PATH, status_code=302)
                response.set_cookie(
                    DASHBOARD_COOKIE,
                    provider.dashboard_cookie(username),
                    max_age=DASHBOARD_COOKIE_TTL,
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    path=DASHBOARD_PATH,
                )
                return response
            return HTMLResponse(
                login_page(
                    DASHBOARD_PATH, subtitle=subtitle, error="Incorrect username or password."
                ),
                status_code=401,
            )
        return HTMLResponse(login_page(DASHBOARD_PATH, subtitle=subtitle))

    return mcp.streamable_http_app()
