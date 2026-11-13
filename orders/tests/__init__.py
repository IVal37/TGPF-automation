"""Safety rails for the whole test suite. Every test run imports this package first.

Tests once launched real Chrome sessions against the live WebJoint, because a test fell through to
the real scraper and its follow-up jobs went to the Redis a real worker was listening to. So:

* the suite refuses to run unless Celery is really on the in-memory broker (orders.test_settings), and
* starting a real Chrome raises RealBrowserBlocked. Scraper tests swap in their own fake Chrome
  (see test_scrapers.py), which replaces this block only for the length of that test.
"""

from unittest.mock import patch

from celery import current_app

import TGPFflows  # noqa: F401  (creates the Celery app)

# the broker Celery will really use, which is not always the Django setting: it prefers the
# CELERY_BROKER_URL environment variable (see orders/test_settings.py)
_broker = str(current_app.conf.broker_url)
if not _broker.startswith("memory://"):
    raise RuntimeError(
        "The tests must run with --settings=orders.test_settings (in-memory broker, dummy logins). "
        f"They were about to use the real broker {_broker!r}."
    )


class RealBrowserBlocked(BaseException):
    """A test tried to start a real Chrome. A BaseException on purpose: the scrapers' and tasks' own
    `except Exception` handlers must not be able to swallow it and let the test pass."""


def _refuse_real_chrome(*args, **kwargs):
    raise RealBrowserBlocked("a test tried to start a real Chrome: mock the scraper, or use FakeChrome")


# started once for the life of the test process; never stopped, deliberately
patch("selenium.webdriver.Chrome", new=_refuse_real_chrome).start()
