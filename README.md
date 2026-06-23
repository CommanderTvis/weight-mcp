# weight-mcp

A personal calorie & protein counter delivered **as an MCP server** for use
inside [claude.ai](https://claude.ai). You log meals from photos or text in a
normal Claude chat; this server counts them, tracks weight, and renders an
interactive dashboard (weight graph + recently eaten) right in the conversation.

Single-user, self-hosted, by design. See [SPEC.md](./SPEC.md) for the rationale.

## How it works

- **MCP server** (Streamable HTTP) exposes tools, a prompt, and an MCP Apps
  dashboard UI to claude.ai.
- **In-process OAuth** gate: claude.ai drives the full OAuth 2.1 + PKCE +
  Dynamic Client Registration flow, but the only human step is entering a single
  shared **password** (configured in `.env`). There are no user accounts.
- **SQLite** stores weights and food logs — one file under `data/`.

## Tools

| Tool | What it does |
| --- | --- |
| `log_food` | Record one eaten item (kcal, protein, …). |
| `record_weight` | Store a body-weight measurement. |
| `lookup_nutrition` | Query public nutrition databases (Open Food Facts, optional USDA). |
| `daily_progress` | Today's intake vs. your goal. |
| `set_goals` | Change the daily calorie/protein targets and floor/ceiling mode. |
| `show_dashboard` | Renders the dashboard inline as an MCP Apps panel (weight graph + recent meals + today's progress). |

## Configure

Copy `.env.example` to `.env` and set at least `WEIGHT_MCP_PASSWORD` and
`WEIGHT_MCP_PUBLIC_BASE_URL`. Nutrition sources default to **Open Food Facts,
filtered to Germany**; set `WEIGHT_MCP_*` to change region or enable USDA.

Goals are not env config — set them from chat with `set_goals` (they persist in
the database). Two modes: `floor` (eat *at least* the target — the default, for
under-eaters) and `ceiling` (stay under — for weight loss). Until you set your
own, the default is 2600 kcal / 150 g protein, floor.

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

Settings → Connectors → add a custom connector, paste your server's base URL
(`https://<your-host>` — the MCP endpoint and OAuth live at the origin root, so
there is no path to append). claude.ai opens the OAuth page; enter your password.
Done — start a chat and tell Claude what you ate.

## Develop

```bash
uv run ruff check .
uv run mypy
uv run pytest
```
