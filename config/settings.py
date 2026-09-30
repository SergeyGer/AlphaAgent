"""Django settings for the AlphaAgent platform.

Configuration is environment-driven (12-factor style) with sane development
defaults that line up with ``docker-compose.yml``.
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & environment
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a permissive boolean environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_required(name: str, *, hint: str = "") -> str:
    """Return a mandatory secret from the environment, or fail loudly.

    No credential is ever hard-coded in this file: anything sensitive must come
    from the environment (see ``.env.example``).
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(
            f"{name} is not set. Copy .env.example to .env and provide a value."
            + (f" {hint}" if hint else "")
        )
    return value


# Debug must be resolved before SECRET_KEY: it decides how strict we are.
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG:
        # Ephemeral per-process key: convenient locally, never a shipped secret.
        # Sessions/tokens will not survive a restart.
        SECRET_KEY = secrets.token_urlsafe(64)
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false. "
            "Generate one with: python -c "
            '"import secrets; print(secrets.token_urlsafe(64))"'
        )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # `daphne` must precede staticfiles so it can install the ASGI runserver.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "channels",
    "rest_framework",
    "rest_framework.authtoken",
    # Local
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database - PostgreSQL (credentials mirror docker-compose.yml)
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "alpha_agent_db"),
        "USER": os.environ.get("POSTGRES_USER", "alpha_user"),
        "PASSWORD": env_required(
            "POSTGRES_PASSWORD",
            hint="It must match the password used by the PostgreSQL service.",
        ),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        "OPTIONS": {
            "connect_timeout": 10,
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Cache - Redis (used for market-data de-duplication, see services/market_data)
# ---------------------------------------------------------------------------
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"{REDIS_URL}/2",
        "KEY_PREFIX": "alphaagent",
        "TIMEOUT": 300,
    }
}

# ---------------------------------------------------------------------------
# Channels - WebSocket fan-out for the live dashboard
#
# The layer rides on the same Redis instance as the broker, on a separate DB
# index so a `FLUSHDB` on the cache never drops live connections.
# ---------------------------------------------------------------------------
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                {
                    "address": f"{REDIS_URL}/3",
                    # The consumer blocks on BZPOPMIN for `brpop_timeout` (5s by
                    # default). Without an explicit read deadline a slow round
                    # trip raises `redis.exceptions.TimeoutError`, which tears
                    # down the WebSocket and makes the dashboard flap between
                    # LIVE and RECONNECTING. Keeping the socket deadline well
                    # above the blocking window removes that race.
                    "socket_timeout": 30,
                    "socket_connect_timeout": 10,
                    "socket_keepalive": True,
                    "health_check_interval": 30,
                }
            ],
            "capacity": 1500,
            # Live UI events are worthless once stale, so they are dropped
            # rather than queued behind a busy consumer.
            "expiry": 10,
        },
    }
}

# ---------------------------------------------------------------------------
# Celery - Redis broker, Celery Beat cron scheduler
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", f"{REDIS_URL}/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", f"{REDIS_URL}/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT", 600)
CELERY_TASK_SOFT_TIME_LIMIT = env_int("CELERY_TASK_SOFT_TIME_LIMIT", 540)
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_RESULT_EXPIRES = timedelta(hours=24)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Allows the test-suite / CI to run the graph without a live worker.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True

# Cron scheduling for the autonomous risk/agent loop.
CELERY_BEAT_SCHEDULE = {
    # Fan-out entrypoint: every autonomous portfolio x watchlist ticker.
    "autonomous-market-monitoring": {
        "task": "tasks.autonomous_market_monitoring_task",
        "schedule": crontab(minute="*/15"),
        "options": {"expires": 900},
    },
    # Equity time series behind the dashboard chart.
    "capture-portfolio-snapshots": {
        "task": "tasks.capture_portfolio_snapshots_task",
        "schedule": crontab(minute="*/15"),
        "options": {"expires": 900},
    },
    # End-of-session risk sweep: enforce the portfolio stop-loss limit.
    "daily-loss-limit-sweep": {
        "task": "tasks.daily_loss_limit_sweep_task",
        "schedule": crontab(hour=21, minute=5, day_of_week="1-5"),
        "options": {"expires": 3600},
    },
    # Expire recommendations nobody acted on.
    "expire-stale-recommendations": {
        "task": "tasks.expire_stale_recommendations_task",
        "schedule": crontab(minute="*/30"),
        "options": {"expires": 1800},
    },
    # Housekeeping: drop expired DRF auth tokens.
    "purge-expired-auth-tokens": {
        "task": "tasks.purge_expired_auth_tokens_task",
        "schedule": crontab(hour=3, minute=30),
        "options": {"expires": 3600},
    },
}

# ---------------------------------------------------------------------------
# DRF - token authentication
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.alphaagent_exception_handler",
}

# ---------------------------------------------------------------------------
# AlphaAgent domain configuration (AI layer / trading universe)
# ---------------------------------------------------------------------------
ALPHA_WATCHLIST = env_list("ALPHA_WATCHLIST", "AAPL,TSLA,BTC")

AI_CONFIG = {
    "PROVIDER": os.environ.get("AI_LLM_PROVIDER", "deepseek").strip().lower(),
    "MODEL": os.environ.get("AI_LLM_MODEL", "deepseek-reasoner").strip(),
    "API_KEY": os.environ.get("AI_LLM_API_KEY", "").strip(),
    "BASE_URL": os.environ.get("AI_LLM_BASE_URL", "").strip(),
    "TEMPERATURE": env_float("AI_LLM_TEMPERATURE", 0.2),
    "MAX_TOKENS": env_int("AI_LLM_MAX_TOKENS", 2048),
    "REQUEST_TIMEOUT": env_int("AI_REQUEST_TIMEOUT", 90),
    "MAX_RETRIES": env_int("AI_MAX_RETRIES", 2),
    "ALLOW_HEURISTIC_FALLBACK": env_bool("AI_ALLOW_HEURISTIC_FALLBACK", True),
    "NEWS_ARTICLE_LIMIT": env_int("AI_NEWS_ARTICLE_LIMIT", 10),
    "PRICE_CACHE_TTL": env_int("AI_PRICE_CACHE_TTL", 60),
    "NEWS_CACHE_TTL": env_int("AI_NEWS_CACHE_TTL", 900),
    # Route the crew's tools through the MCP server instead of calling the
    # service layer in-process. Demonstrates that the same tools serve any MCP
    # client; off by default because in-process calls are one less hop.
    "TOOLS_VIA_MCP": env_bool("AI_TOOLS_VIA_MCP", False),
    "MCP_SERVER_URL": os.environ.get("AI_MCP_SERVER_URL", "").strip(),
    # Providers that need request headers the LLM library omits. The motivating
    # case is Anthropic workspace scoping: an unscoped key is rejected with
    # "must include the anthropic-workspace-id header".
    "WORKSPACE_ID": os.environ.get("AI_LLM_WORKSPACE_ID", "").strip(),
    "EXTRA_HEADERS": os.environ.get("AI_LLM_EXTRA_HEADERS", "").strip(),
}

# USD per 1M tokens, used to populate AgentDecisionLog.api_cost_usd.
AI_MODEL_PRICING = {
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}
AI_DEFAULT_PRICING = {"input": 0.55, "output": 2.19}

# ---------------------------------------------------------------------------
# Human-in-the-loop approvals and Telegram integration
# ---------------------------------------------------------------------------
# How long an AI recommendation stays actionable before it is auto-expired.
# Prices move; a stale proposal is worse than no proposal.
RECOMMENDATION_TTL_MINUTES = env_int("RECOMMENDATION_TTL_MINUTES", 60)

# Sweep throttling. The allocation ceiling is per-trade, so without a cooldown
# two overlapping sweeps can each buy inside their own limit and compound the
# real exposure. Defaults to one Beat interval.
ALPHA_TICKER_COOLDOWN_SECONDS = env_int("ALPHA_TICKER_COOLDOWN_SECONDS", 900)
# Minimum gap between two fan-outs (prevents duplicate dispatch).
ALPHA_SWEEP_DEBOUNCE_SECONDS = env_int("ALPHA_SWEEP_DEBOUNCE_SECONDS", 60)

TELEGRAM_CONFIG = {
    "BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
    # Verifies that webhook calls really came from Telegram.
    "WEBHOOK_SECRET": os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip(),
    "WEBHOOK_URL": os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip(),
    "REQUEST_TIMEOUT": env_int("TELEGRAM_REQUEST_TIMEOUT", 15),
    "MAX_MESSAGE_CHARS": 4000,
}


def _telegram_enabled() -> bool:
    return bool(TELEGRAM_CONFIG["BOT_TOKEN"])


TELEGRAM_CONFIG["ENABLED"] = _telegram_enabled()

# ---------------------------------------------------------------------------
# Security hardening
#
# HTTPS-related settings stay off by default because the bundled Docker stack
# serves plain HTTP on :8000. Enable them when you terminate TLS in front of
# the app (reverse proxy / load balancer) - see docs in the README.
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", False)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Trust the proxy's scheme header only when explicitly enabled.
if env_bool("DJANGO_USE_X_FORWARDED_PROTO", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Cap request body size to bound memory use from oversized uploads.
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DJANGO_MAX_UPLOAD_BYTES", 5 * 1024 * 1024)

# The REST API is token-authenticated; keep the browsable API out of production.
if not DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
        "rest_framework.renderers.JSONRenderer",
    ]

# ---------------------------------------------------------------------------
# Internationalisation / static
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Compiled React SPA (served by core.spa when the build exists).
SPA_DIST_DIR = Path(os.environ.get("SPA_DIST_DIR", BASE_DIR / "frontend" / "dist"))

# ---------------------------------------------------------------------------
# Logging - clean, rotated error/audit logs
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_LEVEL = os.environ.get("DJANGO_LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname:<8} {name}:{lineno} - {message}",
            "style": "{",
        },
        "simple": {"format": "[{asctime}] {levelname:<8} {name} - {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "alphaagent.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "errors.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
            "level": "ERROR",
            "encoding": "utf-8",
        },
    },
    "root": {"handlers": ["console", "file"], "level": _LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console", "file"], "level": _LOG_LEVEL, "propagate": False},
        "django.request": {
            "handlers": ["console", "error_file"],
            "level": "ERROR",
            "propagate": False,
        },
        "alphaagent": {
            "handlers": ["console", "file", "error_file"],
            "level": _LOG_LEVEL,
            "propagate": False,
        },
        "celery": {"handlers": ["console", "file"], "level": _LOG_LEVEL, "propagate": False},
    },
}

# Silence noisy third-party loggers unless explicitly debugging.
for _noisy in ("yfinance", "peewee", "urllib3", "asyncio", "chromadb", "httpx", "LiteLLM"):
    LOGGING["loggers"].setdefault(
        _noisy,
        {"handlers": ["console"], "level": "WARNING", "propagate": False},
    )

# ---------------------------------------------------------------------------
# Test runner speed-up / quieter output
# ---------------------------------------------------------------------------
if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    LOGGING["root"]["level"] = "WARNING"
    LOGGING["loggers"]["alphaagent"]["level"] = "WARNING"
    LOGGING["loggers"]["celery"]["level"] = "WARNING"
