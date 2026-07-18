# weight-mcp — Spec

A personal calorie & protein counter delivered **as an MCP server** for use inside
claude.ai. Self-hosted, multi-user (one admin + admin-registered accounts). This
document only covers the
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

A handful of users, self-hosted, low write volume → SQLite is the correct
default. One file, zero operational overhead, backup = copy the file. No reason
for a client/server DB (Postgres/MySQL) at this scale; it would only add ops
burden.

## Security & auth model

- **One admin + registered users.** The `.env` holds exactly one account: the
  **admin** (username `admin`, password `WEIGHT_MCP_PASSWORD`). All other
  accounts are created by the admin at runtime — via the `register_user`,
  `deregister_user`, and `update_user_password` MCP tools — and stored in the
  database with salted PBKDF2 password hashes. No self-registration, no
  third-party IdP, no email.
- **OAuth gate asks for username + password.** We implement the minimal OAuth
  flow that claude.ai expects when adding an MCP server; the human-facing step
  is a username + password form.
- **Per-user data.** Every row (weights, food logs, goals) is scoped to the
  authenticated account (the token's `sub`); tools never cross accounts.
- **Revocation without token state.** Tokens are stateless JWTs signed with a
  key derived from the admin password and stamped with a digest of the user's
  current password. Updating a user's password (or deregistering them)
  invalidates that user's tokens; rotating the admin password invalidates
  everyone's.

This is "plain OAuth": real enough to satisfy the claude.ai connector handshake,
but the credentials are just the admin `.env` password or an admin-registered
account.

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

All configuration is for the self-hosting admin (other accounts live in the DB):

- **`password`** — the admin account's secret (and the JWT signing root).
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
