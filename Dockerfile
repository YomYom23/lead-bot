# lead-bot/Dockerfile
# One image, two commands. The server runs the default command below; the worker runs the same
# image with `python -m lead_bot.worker --send` (deploy/compose.template.yaml). Each client pins
# its own image tag, which is how one client can be updated before the rest.
#
# Nothing client-specific is baked in: .env, client.toml, the Google key file and the database
# are mounted at run time (.dockerignore keeps them out of the build too).

FROM python:3.12-slim

# uv, copied from its official image so the dependency install matches the lockfile exactly.
# Pinned to the version the lockfile was made with.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

# PYTHONUNBUFFERED: logs reach `docker logs` as they happen, not when a buffer fills.
# UV_COMPILE_BYTECODE: compile once at build time so every container start is faster.
# UV_NO_CACHE: don't keep the download cache in the image; nothing reads it after the build.
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

# A normal user, not root, so a bug in the app can't write outside its own folders. UID 1000 is
# the first normal user on Ubuntu, so the bind-mounted data folder on the VPS can be owned by
# the same number.
RUN useradd --create-home --uid 1000 leadbot \
    && mkdir -p /app /data \
    && chown leadbot:leadbot /app /data

WORKDIR /app
USER leadbot

# Dependencies first, in their own layer: they change rarely, so a code-only change rebuilds in
# seconds instead of reinstalling everything. --frozen refuses to touch uv.lock.
COPY --chown=leadbot:leadbot pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the code. --no-editable installs a real copy into the venv, so the running app doesn't
# depend on the /app/src folder.
COPY --chown=leadbot:leadbot src ./src
RUN uv sync --frozen --no-dev --no-editable

# Where the database lives inside the container. The compose file mounts the client's data
# folder here, so the server and the worker share one SQLite file.
ENV LEAD_BOT_DB_PATH=/data/lead_bot.db

EXPOSE 8000

# `fastapi run` finds the app through [tool.fastapi] in pyproject.toml. It listens on all
# interfaces because the only thing that can reach the container is Caddy, on the Docker network.
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]
