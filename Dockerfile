# syntax=docker/dockerfile:1

# Base images are pinned by digest as well as tag: the tags are mutable, and a
# compromised upstream push would otherwise land straight in a published image.
# To bump, replace both the tag and its digest.

# --- build stage: uv and the build toolchain, never shipped ------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first for better layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install the project itself. --no-editable copies the package into the venv so
# the runtime stage needs no source tree.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- runtime stage: interpreter and the venv, nothing else ------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Debian ships security fixes faster than the upstream python image is rebuilt,
# so patch the pinned base rather than wait for a new digest. This is the one
# deliberately non-reproducible layer: the digest above fixes what we start
# from, this keeps what we ship current.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app

# The venv stays root-owned and read-only to the app user, so a compromised
# process cannot rewrite its own code.
COPY --from=builder --chown=root:root /app/.venv /app/.venv

# Owned by the app user so a fresh named volume mounted here inherits that
# ownership and SQLite can write without the container running as root.
RUN install -d -o app -g app -m 750 /app/data

USER app

EXPOSE 8000
VOLUME ["/app/data"]

# The protected-resource metadata is the one unauthenticated endpoint, so this
# exercises the real event loop rather than just checking the port is open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/.well-known/oauth-protected-resource' % os.environ.get('WEIGHT_MCP_PORT', '8000'), timeout=4).read()"]

CMD ["weight-mcp"]
