# weight-mcp — Spec

A personal calorie & protein counter delivered **as an MCP server** for use inside
claude.ai. Single-user, self-hosted. This document only covers the
distinguishable, non-default decisions; everything unmentioned follows ordinary
conventions.

## Why an MCP server (not a standalone app)

The point is to run the whole experience *inside an existing claude.ai
subscription* — logging meals from photos/text, reasoning about nutrition, and
rendering dashboards all happen in the Claude chat, consuming subscription
tokens instead of standing up a separate product with its own model billing.

This relies on a recent MCP capability: **servers can serve interactive UI**, not
just tools and resources. We use that to show dashboards (weight graph, recent
meals) directly in the conversation.

## Goals

- Primary (the author): sustain a daily **protein and calorie** target — the user
  is an *under*-eater, so the system optimizes for *hitting* intake, not cutting it.
- Secondary (anyone else self-hosting): generic weight management / weight loss.

Implication: targets and "are you on track?" framing must support both
*minimum-floor* goals (eat enough) and *ceiling* goals (eat less). Not deficit-only.

## Architecture

Three components in one deployable:

1. **Web server** — serves the self-rolled OAuth gate and the dashboard UI.
2. **MCP server** — tools, prompt, and UI resources exposed to claude.ai.
3. **Database** — **SQLite**.

### Database choice: SQLite

Single user, self-hosted, low write volume → SQLite is the correct default.
One file, zero operational overhead, backup = copy the file. No reason for a
client/server DB (Postgres/MySQL) at this scale; it would only add ops burden.

## Security & auth model

- **Single-user by design.** The person self-hosting *is* the only user. No
  multi-tenant concerns, no user table, no registration.
- **Password lives in `.env`.** The one secret is a configured password.
- **OAuth gate asks for the password only.** We implement the minimal OAuth flow
  that claude.ai expects when adding an MCP server, but the human-facing step is a
  single password field — no username, no third-party IdP, no email.

This is "plain OAuth": real enough to satisfy the claude.ai connector handshake,
but the credential is just the shared password from `.env`.

## MCP surface

### Prompt
A prompt that primes Claude to act as the calorie/protein counter: how to read
food photos, how to ask for missing detail, how/when to log, and how to surface
the dashboard.

### UI (served by the server, rendered in chat)
- **Weight graph** over time.
- **Recently eaten** — recent logged meals with their nutrition breakdown.
- Intended to be shown e.g. at end of day.

### Tools
- **Record / store weight** — request the user's current weight and persist it.
- **Query nutrition databases** — look up nutrition facts against a configured set
  of *publicly available* nutrition-fact databases.
- **Log a meal** — record what the user ate (from photo-derived or textual report)
  with its calories/protein/etc.

## Configuration

All configuration is for the single self-hosting user:

- **`password`** — the OAuth gate secret.
- **Nutrition-fact database config** — which public DBs to query, and how.
  **Default: pre-filled for a user in Germany** (German/EU food databases out of
  the box). Other regions reconfigure this.

## Usage flow

1. User adds the MCP server on claude.ai. The connector triggers the OAuth page;
   user enters the password; setup is complete.
2. In a normal chat, the user sends **photos of food** or **describes meals in
   text**. The server acts as the counter — logging intake, tracking against the
   protein/calorie goal, recording weight, and showing the dashboard (e.g. at end
   of day).

## Tech & tooling

- **Python**, fully typed, checked with **mypy**.
- **uv** for dependency and environment management (no `pip`, no system Python).

## DevOps

- **GitHub repo** (managed via `gh`).
- **GitHub Actions**:
  - run linters + mypy,
  - build and emit a **Docker image**.
- **Docker Compose**:
  - a **template** compose file for a real/production deployment,
  - a **default** compose file for local build & run.
