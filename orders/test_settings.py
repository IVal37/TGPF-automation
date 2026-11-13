import os

# settings refuses to import without these; give the tests dummy values so
# they run without a populated .env
os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

# Celery prefers the CELERY_BROKER_URL ENVIRONMENT variable over the Django setting, and the
# developer's .env sets it to the real Redis. Setting it here, before settings load .env (which
# never overrides what is already set), is what actually keeps tests off the real queue.
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

from TGPFflows.settings import *  # noqa: E402,F401,F403

# fixed value so the set_webhooks golden trace is the same on every machine
WEBHOOK_SECRET = "test-webhook-secret"

# in-memory SQLite so the suite runs without MySQL
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

# failure screenshots from tests go to a temp folder, never into logs/
import tempfile  # noqa: E402

FAILURE_DIR = os.path.join(tempfile.gettempdir(), "tgpf-test-failures")

# a fixed path, so the golden scraper traces are the same on every machine (Chrome is faked)
TALKROUTE_PROFILE_DIR = "/tmp/tgpf-test-profiles/talkroute"

# ── Nothing a test does may reach a real service ─────────────────────────────
# These override whatever the developer's .env holds, so a test that forgets to mock something
# can neither log in to a real site, text a real customer, nor put a job on the queue a real worker
# is listening to. (orders/tests/__init__.py also refuses to run without these, and blocks Chrome.)
CELERY_BROKER_URL = "memory://"  # see the environment variable above: that is the one Celery reads
HEADLESS = True

WEBJOINT_EMAIL = "test-webjoint-email"
WEBJOINT_PASSWORD = "test-webjoint-password"
TALKROUTE_EMAIL = "test-talkroute-email"
TALKROUTE_PASSWORD = "test-talkroute-password"
WEEDMAPS_EMAIL = "test-weedmaps-email"
WEEDMAPS_PASSWORD = "test-weedmaps-password"
MERCHANT_PAY_EMAIL = "test-merchant-pay-email"
MERCHANT_PAY_PASSWORD = "test-merchant-pay-password"
MERCHANT_PAY_IMAP_USER = ""
MERCHANT_PAY_IMAP_PASS = ""
TALKROUTE_TEST_PHONE = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
NGROK_AUTH_TOKEN = ""
GOOGLE_MAPS_API_KEY = ""
ANTHROPIC_API_KEY = ""
