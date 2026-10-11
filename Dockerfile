# syntax=docker/dockerfile:1

# Base images are pinned by digest as well as tag: the tags are mutable, and a
# compromised upstream push would otherwise land straight in a published image.
# To bump, replace both the tag and its digest.

# --- build stage: uv and the build toolchain, never shipped ------------------
FROM python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS builder

COPY --from=ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 /usr/local/bin/uv /usr/local/bin/uv

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

# Distroless uses /usr/bin rather than the builder's /usr/local/bin.
# Point the venv at the runtime interpreter, keeping the same Python ABI.
RUN sed -i 's|/usr/local/bin|/usr/bin|g' /app/.venv/pyvenv.cfg \
    && ln -sf /usr/bin/python3.13 /app/.venv/bin/python \
    && ln -sf python /app/.venv/bin/python3 \
    && ln -sf python /app/.venv/bin/python3.13 \
    && install -d -o 10001 -g 10001 -m 750 /runtime-data

# --- runtime stage: no shell or package manager ----------------------------
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:83aa8d4f74a4d7f7cf2d472054139bef71a927b76c680c0f2e1021d6b1d6d732

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The venv stays root-owned and read-only to the app user, so a compromised
# process cannot rewrite its own code.
COPY --from=builder --chown=root:root /app/.venv /app/.venv

# Owned by the app user so a fresh named volume mounted here inherits that
# ownership and SQLite can write without the container running as root.
COPY --from=builder --chown=10001:10001 /runtime-data /app/data

USER 10001:10001

EXPOSE 8000
VOLUME ["/app/data"]

# The protected-resource metadata is the one unauthenticated endpoint, so this
# exercises the real event loop rather than just checking the port is open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/.well-known/oauth-protected-resource' % os.environ.get('WEIGHT_MCP_PORT', '8000'), timeout=4).read()"]

ENTRYPOINT ["/app/.venv/bin/python"]
CMD ["-m", "weight_mcp"]
