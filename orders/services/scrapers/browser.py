# imports from django
from django.conf import settings

# imports from selenium
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

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
    except Exception:
        driver.execute_script("arguments[0].click();", el)


# case-insensitive XPath contains() predicate body, e.g.
# f'//button[{ci_contains("save changes")}]'
def ci_contains(text: str, expr: str = ".") -> str:
    return f'contains(translate({expr}, "{_UPPER}", "{_LOWER}"), "{text}")'
