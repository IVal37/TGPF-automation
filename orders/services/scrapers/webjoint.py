# imports from std lib
import difflib
import functools
import inspect
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

# imports from django
from django.conf import settings
from selenium.common.exceptions import TimeoutException, WebDriverException

# imports from selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC

# imports from project
from orders.services.scrapers.browser import (
    BLOCK,
    browser_session,
    ci_contains,
    click,
    click_at_commit,
    click_with_js_fallback,
    fill,
    scroll_into_view,
)
from orders.services.scrapers.errors import BeforeCommitError, at_commit, before_commit, fail
from orders.webhooks import redact_webhook_key, webhook_urls

logger = logging.getLogger(__name__)

_WEBJOINT_URL = "https://order.thegoodpeoplefarms.com/admin/index.html#/"

_ORDERS_NAV_XPATH = "//*[@id='nav_drawer']/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[2]/a"
_ORDER_TITLE = (By.CSS_SELECTOR, '[data-test="page-title"]')
_ORDER_TITLE_RE = re.compile(r"Order\s*#\s*(\S+)")
_SAVE_CHANGES_XPATH = '//button[contains(.,"Save Changes")]'
_CANCEL_XPATH = '//*[text()="Cancel"]'


# raised when the page that opened is not the order we meant to act on. Nothing
# has been written yet, so it is a BeforeCommitError: the task may retry (the
# usual cause is a slow page whose heading never showed) and nothing is ever
# written to the wrong order
class WrongOrderError(BeforeCommitError):
    def __init__(self, expected, seen):
        super().__init__("open order", detail=f"expected order {expected!r} but the page shows {seen!r}")
        self.expected = expected
        self.seen = seen


def _login(driver, wait):
    driver.get(_WEBJOINT_URL)
    fill(wait, (By.CLASS_NAME, "c199.c13"), settings.WEBJOINT_EMAIL)
    fill(wait, (By.CLASS_NAME, "c199.c202.c13"), settings.WEBJOINT_PASSWORD)
    click(wait, (By.CLASS_NAME, "c230.c204.c206.c209.c15"))


# wraps a scraper body in a logged-in WebJoint browser session: launches
# Chrome, logs in (a failed login raises BeforeCommitError("login"): nothing
# has happened yet), calls fn(driver, wait, *args), and always quits the
# browser. Callers just call e.g. fill_order_notes(order_id, notes) — driver/wait
# are injected. If fn takes an order_id, failures name that order, and the
# failure screenshot/page source are saved under it (see browser_session).
def _webjoint_session(fn):
    takes_order = "order_id" in inspect.signature(fn).parameters

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        order_ref = kwargs.get("order_id", args[0] if args else None) if takes_order else None
        with browser_session(
            fn.__name__, order_ref, {"profile.default_content_setting_values.notifications": BLOCK},
        ) as (driver, wait):
            with before_commit("login"):
                _login(driver, wait)
            return fn(driver, wait, *args, **kwargs)
    return wrapper


# Opens one specific order by its WebJoint id (the "<company>-<facility>-
# <customer>-<id>" string, same as in the URL) and checks that the order the
# page shows really is that one; raises WrongOrderError otherwise.
def _open_order(driver, wait, order_id: str):
    if not order_id:
        raise WrongOrderError(order_id, None)

    # the nav drawer only exists once the login has finished; navigating
    # before that would reload the page without a session
    wait.until(EC.presence_of_element_located((By.XPATH, _ORDERS_NAV_XPATH)))

    driver.get(f"{_WEBJOINT_URL}orders/{order_id}/show")
    driver.refresh()  # only the #hash changed, so force a real page load

    def shown_order_id(_):
        for el in driver.find_elements(*_ORDER_TITLE):
            match = _ORDER_TITLE_RE.search(el.text)
            if match:
                return match.group(1)
        return False

    try:
        seen = wait.until(shown_order_id)
    except TimeoutException:
        raise WrongOrderError(order_id, None)
    if seen != order_id:
        raise WrongOrderError(order_id, seen)


# Inventory -> a sub-page in its menu, e.g. "Products" or "Kits"
def _open_inventory_page(wait, page: str):
    click(wait, (By.XPATH, '//*[text()="Inventory"]'))
    click(wait, (By.XPATH, f'//*[text()="{page}"]'))


# `committed`, if given, gets an item appended just before the click: the click
# is the point of no return for whatever the caller is saving
def _click_save_changes(wait, committed=None):
    save_btn = wait.until(EC.element_to_be_clickable((By.XPATH, _SAVE_CHANGES_XPATH)))
    time.sleep(1)
    if committed is not None:
        committed.append(True)
    save_btn.click()


# best-effort close of an open edit modal (no-op if there isn't one)
def _try_cancel(driver):
    try:
        driver.find_element(By.XPATH, _CANCEL_XPATH).click()
    except WebDriverException:
        pass


# The commit point is the click on the notes form's save button. Anything before
# it raises BeforeCommitError (nothing was written, a retry is safe); the save
# click and what follows propagate as-is and are never retried automatically.
@_webjoint_session
def fill_order_notes(driver, wait, order_id: str, order_notes: str):
    save_locator = (By.XPATH, '/html/body/div[4]/div[2]/div[2]/form/div[2]/button')
    with before_commit("open order"):
        _open_order(driver, wait, order_id)

    with before_commit("edit notes"):
        # button is a child of the <p> containing "Order Notes" text
        click(wait, (By.XPATH, f'//p[{ci_contains("order notes", "text()")}]/button'))

        order_notes_input = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[4]/div[2]/div[2]/form/div[1]/div/div/div/div/div/textarea[3]')))
        existing = order_notes_input.get_attribute('value') or ''
        order_notes_input.clear()
        order_notes_input.send_keys(order_notes + ('\n' + existing if existing else ''))

        save_button = wait.until(EC.element_to_be_clickable(save_locator))

    with at_commit("save notes"):
        click_at_commit(wait, save_locator, save_button)

        time.sleep(2)


# The commit point is the first save in the run: a row's Save Changes click, or a
# kit's remove click. Until then a failure raises BeforeCommitError (nothing
# changed, a retry is safe). Row and kit steps that fail are still skipped one by
# one as before; at the end, if any failed, it raises: BeforeCommitError when no
# save had happened yet, otherwise the plain error (never retried automatically,
# since a retry could repeat a save that went through).
@_webjoint_session
def pull_from_chiles(driver, wait, order_id: str):
    with before_commit("open order"):
        _open_order(driver, wait, order_id)

    time.sleep(1)

    committed = []  # an item is added just before each save/remove click
    failures = []   # (step, exception) for each row / kit removal that failed
    try:
        kit_removals = []
        # iterate by index so pass-cases don't re-visit the same row (would cause infinite loop)
        row_count = len(driver.find_elements(By.XPATH, '//tbody/tr'))
        for idx in range(row_count):
            try:
                if not _pull_row(driver, wait, idx, kit_removals, committed):
                    break
            except Exception as e:
                logger.error("pull_from_chiles (order %s): row %d failed: %s", order_id, idx, e)
                failures.append((f"row {idx}", e))
                _try_cancel(driver)

        if kit_removals:
            _remove_from_kits(driver, wait, order_id, kit_removals, committed, failures)

    except Exception as e:
        logger.error("pull_from_chiles (order %s): row loop failed: %s", order_id, e)
        failures.append(("read order rows", e))

    if failures:
        step, exc = failures[0]
        if len(failures) > 1:
            step += f" (+{len(failures) - 1} more)"
        fail(step, exc, bool(committed))


def _pull_row(driver, wait, idx: int, kit_removals: list, committed: list) -> bool:
    """Handle one row of the order's item table: move its pull to Chiles, or note a kit to
    remove it from. Returns False when the table has fewer rows than expected (stop looping)."""
    rows = driver.find_elements(By.XPATH, '//tbody/tr')
    if idx >= len(rows):
        return False  # fewer rows than expected: stop
    row = rows[idx]
    has_chiles = "Chiles Rd" in row.text
    has_shift  = "Starting"  in row.text or "Closing" in row.text
    if has_chiles and not has_shift:
        return True  # purely Chiles, nothing to do
    is_split = has_chiles and has_shift

    edit_btn = row.find_element(By.XPATH, './/button[.//*[local-name()="path"][contains(@d,"M3 17.25")]]')
    scroll_into_view(driver, edit_btn)
    time.sleep(0.5)
    edit_btn.click()

    if is_split:
        # Pulling From is already expanded for multi-kit rows — clicking the
        # header would collapse it, so skip straight to the remove/qty logic.
        wait.until(EC.presence_of_element_located((By.XPATH, '//div[@role="dialog"]')))
        time.sleep(0.5)
    else:
        pulling_from_button = wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//div[@role="button" and .//*[contains(text(),"Pulling From")]]')
        ))
        scroll_into_view(driver, pulling_from_button)
        time.sleep(0.5)
        pulling_from_button.click()
        time.sleep(0.5)

    remove_btns = _modal_remove_buttons(driver)

    if is_split:
        _pull_split_row(driver, wait, remove_btns, committed)
    else:
        _pull_single_row(driver, wait, row, kit_removals, committed)
    return True


def _modal_remove_buttons(driver) -> list:
    """The remove buttons of the open edit dialog, or [] if it has none."""
    try:
        modal_el = driver.find_element(By.XPATH, '//div[@role="dialog"]')
        return modal_el.find_elements(By.CLASS_NAME, "c420")
    except WebDriverException:
        return []


def _pull_split_row(driver, wait, remove_btns: list, committed: list) -> None:
    """A row already pulled from both Chiles and a shift kit: fold the kit's quantity into Chiles."""
    chiles_idx = None
    shift_idx = None
    if len(remove_btns) >= 2:
        for i in range(len(remove_btns)):
            try:
                qty_el = driver.find_element(By.ID, f"packagequantities[{i}].nfrompackagequantity")
                row_text = driver.execute_script(
                    "var e=arguments[0]; while(e && !e.querySelector('.c420')) e=e.parentElement; return e ? e.innerText : '';",
                    qty_el
                )
                if "Chiles Rd" in row_text:
                    chiles_idx = i
                elif "Closing Shift" in row_text or "Starting Shift" in row_text:
                    shift_idx = i
            except WebDriverException:
                pass
    if chiles_idx is not None and shift_idx is not None:
        shift_input = driver.find_element(By.ID, f"packagequantities[{shift_idx}].nfrompackagequantity")
        shift_qty = int(shift_input.get_attribute("value") or "1")
        chiles_input = driver.find_element(By.ID, f"packagequantities[{chiles_idx}].nfrompackagequantity")
        chiles_qty = int(chiles_input.get_attribute("value") or "0")
        chiles_input.clear()
        chiles_input.send_keys(str(chiles_qty + shift_qty))
        time.sleep(0.3)
        remove_btns[shift_idx].click()
        time.sleep(1.5)
        _click_save_changes(wait, committed)
        time.sleep(1)
    else:
        _try_cancel(driver)


def _pull_single_row(driver, wait, row, kit_removals: list, committed: list) -> None:
    """A row pulled from one place: switch it to Chiles if Chiles has stock, otherwise
    queue a removal from the other shift kit."""
    # css- classes from emotion are stable (hashed from styles, not random)
    package_select_control = wait.until(EC.element_to_be_clickable(
        (By.CSS_SELECTOR, 'div.css-1j4kz5m')
    ))
    scroll_into_view(driver, package_select_control)
    time.sleep(0.3)
    package_select_control.click()
    time.sleep(1)

    chiles_opts   = driver.find_elements(By.XPATH, '//*[@role="option" and contains(.,"@ Chiles Rd Shop")]')
    closing_opts  = driver.find_elements(By.XPATH, '//*[@role="option" and contains(.,"@ Closing Shift Mobile Inventory")]')
    starting_opts = driver.find_elements(By.XPATH, '//*[@role="option" and contains(.,"@ Starting Shift Mobile Inventory")]')

    time.sleep(1)

    if chiles_opts:
        chiles_opts[0].click()
        time.sleep(1.5)
        _click_save_changes(wait, committed)
    else:
        item_name = row.text.split("\n")[0].strip()
        if starting_opts:
            kit_removals.append((item_name, "Closing Shift Mobile Inventory"))
        elif closing_opts:
            kit_removals.append((item_name, "Starting Shift Mobile Inventory"))
        click(wait, (By.XPATH, _CANCEL_XPATH))

    time.sleep(1)


def _remove_from_kits(driver, wait, order_id: str, kit_removals: list, committed: list, failures: list) -> None:
    """Remove each queued item from its shift kit on the Kits page."""
    try:
        _open_inventory_page(wait, "Kits")
        for product_name, kit_name in kit_removals:
            try:
                click(wait, (By.XPATH, f'//*[text()="{kit_name}"]'))
                time.sleep(1)
                fill(wait, (By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span/div/div[2]/div/div/div/input'), product_name)
                time.sleep(1)
                try:
                    remove_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span/div/div[2]/table/tbody/tr/td[11]/span/button')))
                except WebDriverException:
                    continue
                committed.append(True)
                remove_btn.click()
                time.sleep(1)
            except Exception as e:
                logger.error("pull_from_chiles (order %s): kit removal failed for %s from %s: %s", order_id, product_name, kit_name, e)
                failures.append((f"kit removal of {product_name}", e))
    except Exception as e:
        logger.error("pull_from_chiles (order %s): kit removal navigation failed: %s", order_id, e)
        failures.append(("open Kits", e))


# The commit point is the click on the assign-driver dialog's confirm button.
# Everything before it raises BeforeCommitError (no driver assigned, a retry is
# safe); the confirm click and what follows propagate as-is, never auto-retried.
@_webjoint_session
def dispatch_to_driver(driver, wait, order_id: str):
    confirm_locator = (By.XPATH, '/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]')
    with before_commit("open order"):
        _open_order(driver, wait, order_id)

    with before_commit("assign driver"):
        driver_button = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/div/div[2]/div/div/div[2]/div/div/div[2]/table/tbody/tr/td[1]/div/label/span[1]/span[1]/input')))
        scroll_into_view(driver, driver_button)
        driver_button.click()
        time.sleep(1)

        click(wait, (By.ID, 'selectActionButton'))
        time.sleep(1)

        click(wait, (By.ID, 'assignDriver'))
        time.sleep(2)

        confirm_button = wait.until(EC.element_to_be_clickable(confirm_locator))
        time.sleep(1.5)

    with at_commit("confirm dispatch"):
        click_at_commit(wait, confirm_locator, confirm_button)
        time.sleep(1)


# the dropdown option whose full rendered text exactly matches `text`, or None.
# Compares .text rather than using XPath text(), which only sees an option's
# direct child text nodes — "Indica" as a bare XPath text() match also matches
# an "Indica Dominant" option if "Dominant" is rendered in a nested span,
# e.g. <li>Indica<span>Dominant</span></li>
def _option_with_text(options, text: str):
    return next((o for o in options if o.text.strip() == text), None)


def _fill_autocomplete(driver, wait, input_el, value: str):
    """Type into a MUI Autocomplete field and select the matching dropdown option.

    Picks the option whose text exactly matches `value`; if none exists, picks
    the 'Create "value"' option MUI shows for a not-yet-existing entry. Falls
    back to Enter only if neither renders (e.g. a plain freeSolo field).
    """
    input_el.clear()
    input_el.send_keys(value)
    time.sleep(0.5)  # let the option list render/filter

    try:
        options = wait.until(EC.presence_of_all_elements_located((By.XPATH, '//*[@role="option"]')))
    except WebDriverException:
        options = []

    exact = _option_with_text(options, value)
    if exact:
        exact.click()
        return

    create_opt = _option_with_text(options, f'Create "{value}"')
    if create_opt:
        create_opt.click()
        return

    input_el.send_keys(Keys.ENTER)


# the product form's autocomplete fields all share the same shape:
# <div id="{field_id}"> wrapping the actual <input>
def _fill_autocomplete_field(driver, wait, field_id: str, value: str):
    input_el = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@id="{field_id}"]//input')))
    _fill_autocomplete(driver, wait, input_el, value)


_CANNABINOID_UNIT_MAP = {"%": "Percentage", "mg": "Mg", "ml": "Ml", "g": "Gram"}


def _parse_cannabinoid_value(value: str) -> Tuple[str, str]:
    """Split a cannabinoid value like '34.5%' or '100mg' into (amount, unit_option).

    unit_option is the exact dropdown label to select: Percentage, Gram, Mg, or Ml.
    """
    match = re.match(r'^\s*([\d.]+)\s*(%|mg|ml|g)\s*$', str(value), re.IGNORECASE)
    if not match:
        raise ValueError(f"unrecognized cannabinoid value: {value!r}")
    amount, unit = match.groups()
    return amount, _CANNABINOID_UNIT_MAP[unit.lower()]


_PRODUCT_NAME_MATCH_THRESHOLD = 0.55

# The fuzzy duplicate-product check is parked (off): set True to skip any invoice
# product that fuzzy-matches one already in WebJoint's Products list.
CHECK_DUPLICATE_PRODUCTS = False


def _normalize_product_name(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', name.lower()).strip()


def _clear_react_input(input_el):
    """Reliably empty a React-controlled MUI text field.

    The search box on the Products page is reused across every product in the
    loop, and plain .clear() only wipes the DOM value — it doesn't always fire
    the input event React needs to update its own controlled state, so the
    previous query can silently survive and get typed-after rather than
    replaced. Select-all + delete fires real key events instead.
    """
    for _ in range(3):
        if input_el.get_attribute("value") == "":
            return
        input_el.send_keys(Keys.CONTROL, "a")
        input_el.send_keys(Keys.DELETE)
        time.sleep(0.2)


def _find_existing_product(driver, wait, name: str) -> Optional[str]:
    """Search the Products list for an existing product matching `name`.

    Webjoint's saved title often varies from the invoice-parsed name (added
    weight/type words, different delimiters), so this fuzzy-matches on the
    normalized text of the search results rather than requiring an exact hit.
    Returns the matching product's name as displayed in webjoint, or None.
    """
    search_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//input[@placeholder="Search Products..."]')))
    query = name.split("|")[0].strip()
    _clear_react_input(search_input)
    search_input.send_keys(query)
    time.sleep(3)  # let the table filter

    target = _normalize_product_name(name)
    best_match, best_ratio = None, 0.0
    for row in driver.find_elements(By.XPATH, '//table/tbody/tr'):
        try:
            candidate = row.find_element(By.XPATH, './td[3]//a').text.strip()
        except WebDriverException:
            continue
        ratio = difflib.SequenceMatcher(None, target, _normalize_product_name(candidate)).ratio()
        if ratio > best_ratio:
            best_match, best_ratio = candidate, ratio

    time.sleep(0.5)

    return best_match if best_ratio >= _PRODUCT_NAME_MATCH_THRESHOLD else None


def _fill_product_form(driver, wait, product_dict: Dict[str, str]) -> None:
    """Open "Add a New Product" and fill in every tab of the form (up to, not including, the submit)."""
    click(wait, (By.XPATH, '//*[text()="Add a New Product"]'))
    time.sleep(0.5)

    fill(wait, (By.XPATH, '//*[@id="name"]'), product_dict["product_name"])
    time.sleep(0.5)

    _fill_autocomplete_field(driver, wait, "brand", product_dict["brand"])
    time.sleep(0.5)

    _fill_autocomplete_field(driver, wait, "product_type", product_dict["product_type"])
    time.sleep(0.5)

    if product_dict["lineage"] != "":
        lineage_dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="lineage"]/div/div[1]/div[2]')))
        scroll_into_view(driver, lineage_dropdown)
        lineage_dropdown.click()
        time.sleep(0.5)
        lineage_options = wait.until(EC.presence_of_all_elements_located((By.XPATH, '//*[@role="option"]')))
        lineage_option = _option_with_text(lineage_options, product_dict["lineage"])
        if lineage_option is None:
            raise ValueError(f'no lineage dropdown option matches {product_dict["lineage"]!r}')
        lineage_option.click()
        time.sleep(0.5)

    if product_dict["strain"] != "":
        _fill_autocomplete_field(driver, wait, "strain", product_dict["strain"])
        time.sleep(0.5)

    _fill_autocomplete_field(driver, wait, "category", product_dict["category"])
    time.sleep(0.5)

    _fill_autocomplete_field(driver, wait, "subcategory", product_dict["subcategory"])
    time.sleep(0.5)

    if product_dict.get("for_sale", "") == "No":
        click(wait, (By.XPATH, '//*[@id="forSale"]//label[2]'))
        time.sleep(0.5)

    if product_dict.get("sale_discount", "") != "0%":
        fill(wait, (By.XPATH, '//*[@id="salepricediscount"]'), product_dict["sale_discount"])
        time.sleep(0.5)

    fill(wait, (By.XPATH, '//*[@id="seoTitle"]'), product_dict["seo_title"])
    time.sleep(0.5)

    description_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span[1]/div/div[2]/div[8]/div/div/div/div[3]/div[1]')))
    description_input.clear()
    time.sleep(0.5)
    description_input.send_keys(product_dict["description"])
    time.sleep(0.5)

    fill(wait, (By.XPATH, '//*[@id="seo_description"]'), product_dict["description"])
    time.sleep(0.5)

    pricing_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[1]/div/div/div/a[2]/span[1]/span/span')))
    scroll_into_view(driver, pricing_button)
    time.sleep(0.3)
    pricing_button.click()
    time.sleep(0.5)

    click(wait, (By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span[2]/div/div/ul/li/span/button/span[1]'))
    time.sleep(0.5)

    fill(wait, (By.XPATH, '//*[@id="variants[0].unitweight"]'), product_dict["weight_size"])
    time.sleep(0.5)

    click(wait, (By.XPATH, '//*[@id="unit"]/div/div[1]/div[2]'))
    click(wait, (By.XPATH, f'//*[@role="option" and text()="{product_dict["unit"]}"]'))
    time.sleep(0.5)

    fill(wait, (By.XPATH, '//*[@id="variants[0].price"]'), product_dict["price"])
    time.sleep(0.5)

    click(wait, (By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[1]/div/div/div/a[3]/span[1]/span/span'))
    time.sleep(0.5)

    for idx, (cannabinoid, value) in enumerate(product_dict.get("cannabinoids", {}).items()):
        _add_cannabinoid(driver, wait, idx, cannabinoid, value)

    time.sleep(1)


def _add_cannabinoid(driver, wait, idx: int, cannabinoid: str, value: str) -> None:
    """Add one cannabinoid row (type, unit, min and, for plain types, max) to the product form."""
    amount, unit_option = _parse_cannabinoid_value(value)

    click(wait, (By.XPATH,
        '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span[3]'
        f'//button[{ci_contains("add")}]'
    ))
    time.sleep(0.5)

    # each added row gets its own (non-unique) id="type"/"id="unit"; the
    # most recently added row is always the last match in the DOM
    click(wait, (By.XPATH, '(//*[@id="type"])[last()]/div/div[1]/div[2]'))
    click(wait, (By.XPATH, f'//*[@role="option" and text()="{cannabinoid}"]'))
    time.sleep(0.5)

    cannabinoid_unit_dropdown = wait.until(EC.element_to_be_clickable(
        (By.XPATH, '(//*[@id="unit"])[last()]//div[contains(@class, "indicatorContainer")]')
    ))
    scroll_into_view(driver, cannabinoid_unit_dropdown)
    time.sleep(0.3)
    cannabinoid_unit_dropdown.click()
    click(wait, (By.XPATH, f'//*[@role="option" and text()="{unit_option}"]'))
    time.sleep(0.5)

    fill(wait, (By.XPATH, f'//*[@id="cannabinoids[{idx}].min"]'), amount)
    time.sleep(0.5)

    # "Total X" cannabinoid types (e.g. "Total THC") only render a single
    # "Total" field (the min input above) — there's no max input in the DOM
    # at all for these, unlike plain types (e.g. "THC") which show min/max
    if not cannabinoid.strip().lower().startswith("total"):
        fill(wait, (By.XPATH, f'//*[@id="cannabinoids[{idx}].max"]'), amount)
        time.sleep(0.5)


def _submit_product(driver, wait, committed: list) -> None:
    """Click the product form's submit button. The click is this product's commit point."""
    # document.getElementById("name") isn't unique on this SPA — other mounted
    # routes/forms elsewhere in the DOM also have a "name" field, so relying on
    # its .form property can silently grab the wrong (hidden) form. Instead,
    # gather every type=submit button matching the visible label and pick
    # whichever one is actually displayed and enabled — that's always the one
    # in the currently-visible product form.
    def _visible_submit_button(_):
        candidates = driver.find_elements(By.XPATH,
            f'//button[@type="submit"][{ci_contains("create product")} or {ci_contains("save changes")}]'
        )
        for btn in candidates:
            if btn.is_displayed() and btn.is_enabled():
                return btn
        return False

    try:
        save_product_button = wait.until(_visible_submit_button)
    except TimeoutException:
        candidates = driver.find_elements(By.XPATH, '//button[@type="submit"]')
        logger.error(
            "create_product: no visible+enabled submit button found — candidates=%s",
            [
                (driver.execute_script("return arguments[0].outerHTML;", b), b.is_displayed(), b.is_enabled())
                for b in candidates
            ],
        )
        raise
    scroll_into_view(driver, save_product_button)
    time.sleep(1)
    committed.append(True)
    click_with_js_fallback(driver, save_product_button)

    time.sleep(2)


# The commit point is each product's submit click. A product that fails before
# its submit is skipped as before (the others continue). At the end, if any
# failed: BeforeCommitError when no product had been submitted yet (a retry
# cannot duplicate anything), otherwise the plain error (never retried
# automatically, since a retry would create the earlier products again).
@_webjoint_session
def create_product(driver, wait, product_dicts: List[Dict[str, str]]):
    with before_commit("open Products"):
        _open_inventory_page(wait, "Products")
        time.sleep(1)

    committed = []  # an item is added just before each product's submit click
    failures = []   # (product name, exception) for each product that failed

    for product_dict in product_dicts:
        try:
            if CHECK_DUPLICATE_PRODUCTS:
                existing = _find_existing_product(driver, wait, product_dict["product_name"])
                if existing:
                    logger.info(
                        "create_product: skipping %r — fuzzy-matches existing product %r",
                        product_dict["product_name"], existing,
                    )
                    continue

            _fill_product_form(driver, wait, product_dict)

            _submit_product(driver, wait, committed)

        except Exception as e:
            logger.exception("create_product: product creation failed: %s", e)
            failures.append((product_dict.get("product_name", "?"), e))
            continue

    if failures:
        name, exc = failures[0]
        step = f"create product {name!r}"
        if len(failures) > 1:
            step += f" (+{len(failures) - 1} more)"
        fail(step, exc, bool(committed))


@_webjoint_session
def set_webhooks(driver, wait, base_url: str) -> None:
    # the three webhook routes live under a secret path segment (WEBHOOK_SECRET)
    urls = webhook_urls(base_url)
    new_order_url = urls["new_order"]
    complete_order_url = urls["complete_order"]
    cancel_order_url = urls["cancel_order"]

    # the commit point is the Save Changes click; navigation and filling the
    # three URL fields before it raise BeforeCommitError
    with before_commit("open order settings"):
        click(wait, (By.XPATH, '//*[@id="nav_drawer"]/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[9]'))
        click(wait, (By.XPATH, '//*[@id="nav_drawer"]/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[9]/div[2]/div/div/menu/a[1]'))
        click(wait, (By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div/div[6]/div[1]'))

    failure = None
    try:
        with before_commit("fill webhook URLs"):
            fill(wait, (By.ID, "hooks.orderSubmit"), new_order_url)
            fill(wait, (By.ID, "hooks.orderComplete"), complete_order_url)
            fill(wait, (By.ID, "hooks.orderCancel"), cancel_order_url)

            time.sleep(1)

            # match by button text — the old positional XPath (form/div/div[16]/div[1]/button)
            # broke whenever the settings form layout shifted by a div
            save_changes_button = wait.until(EC.element_to_be_clickable((
                By.XPATH, f'//button[{ci_contains("save changes")}]',
            )))
            scroll_into_view(driver, save_changes_button)
            time.sleep(1)

        with at_commit("save webhooks"):
            click_with_js_fallback(driver, save_changes_button)
            logger.info(
                "set_webhooks: saved — new_order=%s complete_order=%s",
                redact_webhook_key(new_order_url), redact_webhook_key(complete_order_url),
            )
    except Exception as e:
        failure = e

    # the wait after this step is kept whether or not it worked, as before
    time.sleep(5)
    if failure is not None:
        raise failure

