# AlphaAgent application image.
#
# Serves all three application roles from one image:
#   web    -> gunicorn config.wsgi:application
#   worker -> celery -A config worker
#   beat   -> celery -A config beat
#
# Build:  docker build -t alphaagent:latest .
# Run:    docker compose up -d

# ---------------------------------------------------------------------------
# Stage 1 - build the React SPA.
#
# `frontend/dist` is gitignored (build output does not belong in a repository),
# so the image must produce it. Building here means `docker compose up --build`
# from a fresh clone yields a working dashboard, with no manual npm step.
# ---------------------------------------------------------------------------
FROM node:26-alpine AS frontend

WORKDIR /ui

# Dependencies first so this layer caches across source edits.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 2 - Python runtime.
# ---------------------------------------------------------------------------
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq-dev + gcc: psycopg2 / lxml native builds on architectures without
# manylinux wheels. curl: container healthchecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The compiled dashboard, built in stage 1. Copied after `COPY . .` so it is
# always the freshly built bundle, never whatever happened to be on disk.
COPY --from=frontend /ui/dist ./frontend/dist

# Writable paths for logs and CrewAI's store (see runtime_env.py).
RUN mkdir -p /app/logs /app/.runtime

# Fail the build early on a syntax error anywhere in the project.
RUN python -m compileall -q ai_agent.py tasks.py telegram_bot.py runtime_env.py \
        config core services mcp_server

EXPOSE 8000

# Daphne (ASGI) is required: the live dashboard streams over WebSocket, which a
# WSGI server such as gunicorn cannot serve.
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
