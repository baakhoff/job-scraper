# syntax=docker/dockerfile:1

# ---- builder: resolve & install the project into system site-packages -------
FROM python:3.12-slim AS builder

# uv for fast, reproducible installs (static binary, no Python bootstrap).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

# Copy only what's needed to build the wheel so this layer caches well.
COPY pyproject.toml README.md ./
COPY src ./src
COPY main.py config.py ./

# Installs the package *and* its dependencies into /usr/local (system python).
# lxml et al. ship manylinux wheels, so no compiler/toolchain is required.
RUN uv pip install --system .

# ---- runtime: minimal, non-root --------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LJP_DB_PATH=/data/jobs.db

# Bring over the installed deps and our `src` package; nothing else from builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages \
                    /usr/local/lib/python3.12/site-packages

WORKDIR /app

# Top-level entry modules are not part of the wheel, so copy them explicitly.
COPY main.py config.py web.py ./
# The web UI's HTML template is served at runtime by web.py (FileResponse).
COPY templates ./templates
COPY docker/ ./docker/

# Non-root user. /data is the DB volume mount point and must be writable by it;
# an empty named volume inherits this ownership on first mount.
RUN useradd --create-home --uid 10001 appuser \
    && chmod +x docker/*.sh \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

USER appuser

VOLUME ["/data"]

# Default: run a single search built from LJP_SEARCH_* env vars. Override the
# command to run any CLI verb, e.g. `docker compose run --rm parser \
# python main.py list --limit 20`.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
