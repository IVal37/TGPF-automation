# imports from std lib
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# imports from django
from django.conf import settings

# imports from selenium
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# imports from project
from orders.services.scrapers.errors import BeforeCommitError, failed_step

logger = logging.getLogger(__name__)

# failure screenshots / page sources older than this are deleted whenever a new one is written
FAILURE_KEEP_DAYS = 14

# Chrome content-setting value meaning "block" (used in prefs dicts)
BLOCK = 2

_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"


# launches Chrome with the shared window size / headless setting every
# scraper uses, plus any scraper-specific content-setting prefs
def create_driver(prefs: dict = None):
    opts = Options()
    if prefs:
        opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--window-size=1280,720")
    if settings.HEADLESS:
        opts.add_argument("--headless=new")
    driver = webdriver.Chrome(options=opts)
    # React re-renders can invalidate an element mid-wait; retry instead of raising
    return driver, WebDriverWait(
        driver, 15,
        ignored_exceptions=(NoSuchElementException, StaleElementReferenceException),
    )


# waits for an element to be clickable, then clicks it (re-locating once if
# the page re-rendered between the wait and the click)
def click(wait, locator):
    for attempt in range(3):
        el = wait.until(EC.element_to_be_clickable(locator))
        try:
            el.click()
            return el
        except StaleElementReferenceException:
            if attempt == 2:
                raise


# waits for an input to be clickable, then replaces its value (same stale retry)
def fill(wait, locator, value):
    for attempt in range(3):
        el = wait.until(EC.element_to_be_clickable(locator))
        try:
            el.clear()
            el.send_keys(value)
            return el
        except StaleElementReferenceException:
            if attempt == 2:
                raise


def scroll_into_view(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)


# native click, falling back to a JS click when something overlays the element
def click_with_js_fallback(driver, el):
    try:
        el.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", el)


# case-insensitive XPath contains() predicate body, e.g.
# f'//button[{ci_contains("save changes")}]'
def ci_contains(text: str, expr: str = ".") -> str:
    return f'contains(translate({expr}, "{_UPPER}", "{_LOWER}"), "{text}")'


def click_at_commit(wait, locator, el):
    """Clicks `el`, which the caller located BEFORE its commit point. A stale
    element means the click did not happen, so re-locating and clicking again
    cannot double-act."""
    for attempt in range(3):
        try:
            el.click()
            return el
        except StaleElementReferenceException:
            if attempt == 2:
                raise
            el = wait.until(EC.element_to_be_clickable(locator))


def _now() -> datetime:
    return datetime.now()


def _safe(part) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(part))


# writes <label>-<order_ref>-<timestamp>.png/.html under settings.FAILURE_DIR and
# deletes anything there older than FAILURE_KEEP_DAYS. Best effort: a problem here
# is logged and never replaces the error being reported. The folder can hold
# customer details, so it lives under the gitignored logs/ directory.
def save_failure_artifacts(driver, label: str, order_ref=None) -> None:
    try:
        folder = Path(settings.FAILURE_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{_safe(label)}-{_safe(order_ref or 'none')}-{_now().strftime('%Y%m%d-%H%M%S')}"
        try:
            driver.save_screenshot(str(folder / f"{stem}.png"))
        except Exception as e:
            logger.warning("%s: could not save failure screenshot: %s", label, e)
        try:
            (folder / f"{stem}.html").write_text(driver.page_source, encoding="utf-8")
        except Exception as e:
            logger.warning("%s: could not save failure page source: %s", label, e)

        cutoff = (_now() - timedelta(days=FAILURE_KEEP_DAYS)).timestamp()
        for old in folder.iterdir():
            try:
                if old.is_file() and old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception as e:
        logger.warning("%s: could not save failure artifacts: %s", label, e)


# Launches Chrome for one scraper run and always quits it. If anything raises
# inside the block, a screenshot and the page source are saved first (named for
# the scraper and `order_ref`, never a phone number) and the error is logged and
# re-raised for the task layer to classify.
@contextmanager
def browser_session(label: str, order_ref=None, prefs: dict = None):
    try:
        driver, wait = create_driver(prefs)
    except Exception as e:
        raise BeforeCommitError("start browser", e) from e
    try:
        yield driver, wait
    except Exception as exc:
        save_failure_artifacts(driver, label, order_ref)
        where = f" (order {order_ref})" if order_ref else ""
        logger.error("%s%s: failed at step '%s': %s", label, where, failed_step(exc), exc)
        raise
    finally:
        driver.quit()
