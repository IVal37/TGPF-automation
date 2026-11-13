"""Django settings for TGPFflows.

Every environment variable the project reads is read here (from the process
environment or .env, see .env.example); the rest of the code uses
django.conf.settings, never os.environ.
"""

import os
from pathlib import Path

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from orders.services.hours import SUNDAY_CLOSE, WEEKDAY_CLOSE

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() == "true"


SECRET_KEY = env_str("DJANGO_SECRET_KEY")

# the webhook routes are served at hooks/<WEBHOOK_SECRET>/..., so anyone without
# this value can't post fake orders (see orders/webhooks.py)
WEBHOOK_SECRET = env_str("WEBHOOK_SECRET")

# refuse to start (web server, Celery worker and beat all import settings)
# rather than run with an empty secret
for _name, _value in (("DJANGO_SECRET_KEY", SECRET_KEY), ("WEBHOOK_SECRET", WEBHOOK_SECRET)):
    if not _value:
        raise ImproperlyConfigured(f"{_name} is not set - add it to .env (see .env.example)")

# run Chrome without a window
HEADLESS = env_bool("HEADLESS")
# suppresses every SMS and WebJoint write (see CLAUDE.md)
RESTOCK_TEST_MODE = env_bool("RESTOCK_TEST_MODE")

# never on in production or behind a tunnel
DEBUG = env_bool("DJANGO_DEBUG")

# the app is reached through an ngrok tunnel; add your ngrok domain here if
# it isn't one of these two
ALLOWED_HOSTS = ["localhost", "127.0.0.1", ".ngrok-free.app", ".ngrok.app"]

# ngrok forwards requests to Django as plain HTTP, so Django's CSRF Origin
# check has to be told the public https origins are legitimate
CSRF_TRUSTED_ORIGINS = ["https://*.ngrok-free.app", "https://*.ngrok.app"]

# dashboard login (django.contrib.auth.urls is mounted at accounts/)
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "orders",
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

ROOT_URLCONF = "TGPFflows.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "TGPFflows.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env_str("DB_NAME", "tgpfflows"),
        "USER": env_str("DB_USER", "root"),
        "PASSWORD": env_str("DB_PASSWORD"),
        "HOST": env_str("DB_HOST", "localhost"),
        "PORT": env_str("DB_PORT", "3306"),
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"

TIME_ZONE = "America/Los_Angeles"

USE_I18N = True

USE_TZ = False


STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        # masks the secret in /hooks/<key>/... paths before anything is printed
        "redact_webhook_key": {"()": "orders.webhooks.RedactWebhookKeyFilter"},
    },
    "formatters": {
        "simple": {
            "format": "[{levelname}] {name}: {message}",
            "style": "{",
        },
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[{server_time}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "filters": ["redact_webhook_key"],
        },
        # same as Django's default runserver access-log handler, plus the filter
        "django.server": {
            "class": "logging.StreamHandler",
            "formatter": "django.server",
            "filters": ["redact_webhook_key"],
        },
    },
    "loggers": {
        "orders": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["django.server"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# Celery — browser work plus the daily Telegram restock digests. How to start
# the workers and beat: RUNBOOK.md.

CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = False

# Every task that opens a Chrome session (WebJoint, TalkRoute, Weedmaps, merchant
# pay) goes to the 'browser' queue, which a single-concurrency worker drains one
# job at a time, so two sessions never hit the same account at once. Add any new
# scraper-backed task here. The restock digests stay on the default queue.
BROWSER_TASKS = (
    "send_message_task",
    "fill_order_notes_task",
    "pull_from_chiles_task",
    "dispatch_to_driver_task",
    "prepare_order_task",
    "resolve_payment_type_task",
    "create_product_task",
)
CELERY_TASK_ROUTES = {f"orders.tasks.{name}": {"queue": "browser"} for name in BROWSER_TASKS}
# one job per worker slot at a time, so a queued browser job is never held
# back behind a long-running one on the same worker
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# deliberately NOT acks_late: a task that dies mid-run must not be re-run, since
# a re-run send_message_task would text the customer twice

CELERY_BEAT_SCHEDULE = {
    # 3rd St storefront closes at 8:30pm every day
    "send-third-st-restock": {
        "task": "orders.tasks.send_third_st_restock",
        "schedule": crontab(hour=20, minute=30),
    },
    # the delivery side's closing hours come from orders/services/hours.py
    "send-delivery-restock-weekday": {
        "task": "orders.tasks.send_delivery_restock",
        "schedule": crontab(hour=WEEKDAY_CLOSE, minute=0, day_of_week="mon,tue,wed,thu,fri,sat"),
    },
    "send-delivery-restock-sunday": {
        "task": "orders.tasks.send_delivery_restock",
        "schedule": crontab(hour=SUNDAY_CLOSE, minute=0, day_of_week="sun"),
    },
}

# Google Maps Distance Matrix key for real driving times (orders/services/travel.py).
# Unset: the stored travel-time table is used and nothing is sent to Google.
GOOGLE_MAPS_API_KEY = env_str("GOOGLE_MAPS_API_KEY")

# screenshots and page sources saved when a browser scraper fails; deleted after
# 14 days. Under the gitignored logs/ directory because pages can show customer details.
FAILURE_DIR = BASE_DIR / "logs" / "failures"

# ── Credentials for the sites the scrapers log in to ─────────────────────────
WEBJOINT_EMAIL = env_str("WEBJOINT_EMAIL")
WEBJOINT_PASSWORD = env_str("WEBJOINT_PASSWORD")
TALKROUTE_EMAIL = env_str("TALKROUTE_EMAIL")
TALKROUTE_PASSWORD = env_str("TALKROUTE_PASSWORD")
# Chrome profile folder the TalkRoute scraper keeps its login in, so it does not have to log in
# (and pass Cloudflare's "verify you are human" check) every time. Set up by hand with
# `manage.py talkroute_login`. It holds a live login, so it is gitignored.
TALKROUTE_PROFILE_DIR = env_str("TALKROUTE_PROFILE_DIR") or str(BASE_DIR / ".browser_profiles" / "talkroute")
# a phone number that is safe to text while trying things out (manage.py send_test_message)
TALKROUTE_TEST_PHONE = env_str("TALKROUTE_TEST_PHONE")
WEEDMAPS_EMAIL = env_str("WEEDMAPS_EMAIL")
WEEDMAPS_PASSWORD = env_str("WEEDMAPS_PASSWORD")
MERCHANT_PAY_EMAIL = env_str("MERCHANT_PAY_EMAIL")
MERCHANT_PAY_PASSWORD = env_str("MERCHANT_PAY_PASSWORD")
# the mailbox that receives merchant pay's one-time verification codes
MERCHANT_PAY_IMAP_USER = env_str("MERCHANT_PAY_IMAP_USER")
MERCHANT_PAY_IMAP_PASS = env_str("MERCHANT_PAY_IMAP_PASS")

# ── Other services ───────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = env_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env_str("TELEGRAM_CHAT_ID")
NGROK_AUTH_TOKEN = env_str("NGROK_AUTH_TOKEN")
# invoice reading (orders/services/invoice.py); empty key falls back to the SDK's own lookup
ANTHROPIC_API_KEY = env_str("ANTHROPIC_API_KEY")
INVOICE_MODEL = env_str("INVOICE_MODEL") or "claude-opus-4-8"
