# weight-mcp

A personal calorie & protein counter delivered as an MCP server for use
inside [claude.ai](https://claude.ai). You log meals from photos or text in a
normal Claude chat; this server counts them, tracks weight, and renders an
interactive dashboard (weight graph + recently eaten) right in the conversation.

Self-hosted, multi-user: one **admin** account lives in `.env`, and the admin
registers further accounts from chat — each user sees only their own data. See
[SPEC.md](./SPEC.md) for the rationale.

## How it works

- MCP server (Streamable HTTP) exposes tools, a prompt, and an MCP Apps
  dashboard UI to claude.ai.
- In-process OAuth gate: claude.ai drives the full OAuth 2.1 + PKCE +
  Dynamic Client Registration flow; the only human step is a username + password
  form. The admin signs in as `admin` with the password from `.env`; everyone
  else signs in with an account the admin registered.
- SQLite stores accounts, weights, and food logs — one file under `data/`.
  Every data row is scoped to the account that logged it.

## Tools

| Tool | What it does |
| --- | --- |
| `log_food` | Record an eaten item (kcal, protein, …), numbered per day; re-logging a meal number overwrites it (edits). |
| `delete_food` | Remove one of today's meals by its number. |
| `record_weight` | Store a body-weight measurement. |
| `lookup_nutrition` | Query public nutrition databases (Open Food Facts, optional USDA). |
| `daily_progress` | Today's intake vs. your goal. |
| `set_goals` | Change the daily calorie/protein targets and floor/ceiling mode. |
| `show_dashboard` | Renders the dashboard inline as an MCP Apps panel (weight graph + recent meals + today's progress). |

All of the above operate on the calling account's own data. The admin account
additionally gets user management:

| Tool (admin only) | What it does |
| --- | --- |
| `register_user` | Create a non-admin account (username + password, stored hashed in the DB). |
| `deregister_user` | Remove an account and revoke its tokens (its logged data is kept). |
| `update_user_password` | Set a new password for an account, invalidating its existing tokens. |

## Configure

Copy `.env.example` to `.env` and set at least `WEIGHT_MCP_PASSWORD` (the
admin account's password — the only account configured in `.env`) and
`WEIGHT_MCP_PUBLIC_BASE_URL`. Non-admin accounts are not env config: the admin
creates them from chat with `register_user`. Nutrition sources default to Open Food Facts,
filtered to Germany; set `WEIGHT_MCP_*` to change region or enable USDA.

Goals are not env config — set them from chat with `set_goals` (they persist in
the database). Two modes: `floor` (eat *at least* the target — the default, for
under-eaters) and `ceiling` (stay under — for weight loss). Until you set your
own, the default is 2600 kcal / 150 g protein, floor. Goals are per account,
like all other data.

## Run

Local (dev):

```bash
uv sync
cp .env.example .env   # then edit
uv run weight-mcp
```

Docker (local build):

```bash
docker compose up --build
```

For a real deployment you need public HTTPS (claude.ai connects from Anthropic's
cloud, not your device). Copy `docker-compose.template.yml`, put the server
behind a TLS reverse proxy, and set `WEIGHT_MCP_PUBLIC_BASE_URL` to that origin.

## Add to claude.ai

Customize → Connectors → "Add custom connector" (on Team/Enterprise:
Organization settings → Connectors → Add → Custom → Web), paste your server's
base URL (`https://<your-host>` — the MCP endpoint and OAuth live at the origin
root, so there is no path to append). claude.ai opens the OAuth page; sign in
with your username and password (`admin` + the `.env` password for the admin, or
credentials the admin registered for you). Done — start a chat and tell Claude
what you ate.

## Develop

```bash
uv run ruff check .
uv run mypy
uv run pytest
```
