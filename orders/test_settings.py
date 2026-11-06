from TGPFflows.settings import *  # noqa: F401,F403

# in-memory SQLite so the suite runs without MySQL
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
