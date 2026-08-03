# Runtime image for all four processes of the self-hosted sandboxed coding agent
# (worker / session-manager / server / preview-proxy). ONE image — each docker-compose service just
# overrides `command`. Built + pushed to GHCR by .github/workflows/build-image.yml.
#
# The tools run in Daytona's CLOUD (not in this container), so there is no Docker-in-Docker here —
# the worker only needs DAYTONA_API_KEY at runtime.
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_SYNC=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# uv shells out to `git` to fetch the harness git dependency (the uv base image has no git).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies only (pyproject has `package = false`). Cached unless the lock/spec changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Put the venv on PATH so `python` is the project interpreter (with the harness, deps, and aiohttp).
ENV PATH="/app/.venv/bin:$PATH"

# Headless Chromium for the preview gallery's screenshots (preview/screenshots.py). Only the
# preview-proxy service uses it, but all four share this image, so it's installed once here.
# `chromium-headless-shell` is Playwright's stripped headless build — a fraction of the full
# chromium download, since we only ever render a page and grab a jpeg. `--with-deps` pulls the
# system libraries Chromium needs. Screenshots degrade to placeholder tiles if this layer is
# removed, so you can drop it to slim the image (also set PREVIEW_SCREENSHOTS=0 to skip the
# launch attempt entirely).
RUN playwright install --with-deps chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*

# The example source, imported as the `examples.*` namespace package off PYTHONPATH=/app.
COPY examples/ ./examples/

# Overridden per compose service; a sane default so `docker run <image>` starts the agent worker.
CMD ["python", "-m", "examples.sandbox_tools.coding_agent.worker"]
