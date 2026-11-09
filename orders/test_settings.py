import os

# settings refuses to import without these; give the tests dummy values so
# they run without a populated .env
os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

from TGPFflows.settings import *  # noqa: E402,F401,F403

# fixed value so the set_webhooks golden trace is the same on every machine
WEBHOOK_SECRET = "test-webhook-secret"

# in-memory SQLite so the suite runs without MySQL
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
