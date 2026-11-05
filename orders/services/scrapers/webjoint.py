# imports from std lib
import difflib
import functools
import logging
import os
import re
import time
from typing import Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)

# imports from selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# imports from project
from orders.services.scrapers.browser import (
    BLOCK, ci_contains, click, click_with_js_fallback, create_driver, fill, scroll_into_view,
)

_WEBJOINT_URL = "https://order.thegoodpeoplefarms.com/admin/index.html#/"

_ORDERS_NAV_XPATH = "//*[@id='nav_drawer']/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[2]/a"
_NEWEST_ORDER_XPATH = '//*[@id="root"]/div/div/main/div[2]/div/div[1]/div[2]/table/tbody/tr[1]/td[2]/span'
_SAVE_CHANGES_XPATH = '//button[contains(.,"Save Changes")]'
_CANCEL_XPATH = '//*[text()="Cancel"]'


def _login(driver, wait):
    driver.get(_WEBJOINT_URL)
    fill(wait, (By.CLASS_NAME, "c199.c13"), os.environ.get('WEBJOINT_EMAIL', ''))
    fill(wait, (By.CLASS_NAME, "c199.c202.c13"), os.environ.get('WEBJOINT_PASSWORD', ''))
    click(wait, (By.CLASS_NAME, "c230.c204.c206.c209.c15"))


# wraps a scraper body in a logged-in WebJoint browser session: launches
# Chrome, logs in (logging "<fn name>: login failed" and bailing if that
# fails), calls fn(driver, wait, *args), and always quits the browser.
# Callers just call e.g. fill_order_notes(notes) — driver/wait are injected.
def _webjoint_session(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        driver, wait = create_driver({"profile.default_content_setting_values.notifications": BLOCK})
        try:
            try:
                _login(driver, wait)
            except Exception as e:
                logger.error("%s: login failed: %s", fn.__name__, e)
                return
            return fn(driver, wait, *args, **kwargs)
        finally:
            driver.quit()
    return wrapper


# Orders page -> top row of the orders table (the most recently placed order)
def _open_newest_order(wait):
    click(wait, (By.XPATH, _ORDERS_NAV_XPATH))
    click(wait, (By.XPATH, _NEWEST_ORDER_XPATH))


# Inventory -> a sub-page in its menu, e.g. "Products" or "Kits"
def _open_inventory_page(wait, page: str):
    click(wait, (By.XPATH, '//*[text()="Inventory"]'))
    click(wait, (By.XPATH, f'//*[text()="{page}"]'))


def _click_save_changes(wait):
    save_btn = wait.until(EC.element_to_be_clickable((By.XPATH, _SAVE_CHANGES_XPATH)))
    time.sleep(1)
    save_btn.click()


# best-effort close of an open edit modal (no-op if there isn't one)
def _try_cancel(driver):
    try:
        driver.find_element(By.XPATH, _CANCEL_XPATH).click()
    except Exception:
        pass


@_webjoint_session
def fill_order_notes(driver, wait, order_notes: str):
    try:
        _open_newest_order(wait)

        # button is a child of the <p> containing "Order Notes" text
        click(wait, (By.XPATH, f'//p[{ci_contains("order notes", "text()")}]/button'))

        order_notes_input = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[4]/div[2]/div[2]/form/div[1]/div/div/div/div/div/textarea[3]')))
        existing = order_notes_input.get_attribute('value') or ''
        order_notes_input.clear()
        order_notes_input.send_keys(order_notes + ('\n' + existing if existing else ''))

        click(wait, (By.XPATH, '/html/body/div[4]/div[2]/div[2]/form/div[2]/button'))

        time.sleep(2)

    except Exception as e:
        logger.error("fill_order_notes: navigation/notes step failed: %s", e)


@_webjoint_session
def pull_from_chiles(driver, wait):
    try:
        _open_newest_order(wait)
    except Exception as e:
        logger.error("pull_from_chiles: navigation failed: %s", e)
        return

    time.sleep(1)

    try:
        kit_removals = []
        # iterate by index so pass-cases don't re-visit the same row (would cause infinite loop)
        row_count = len(driver.find_elements(By.XPATH, '//tbody/tr'))
        for idx in range(row_count):
            try:
                rows = driver.find_elements(By.XPATH, '//tbody/tr')
                if idx >= len(rows):
                    break
                row = rows[idx]
                has_chiles = "Chiles Rd" in row.text
                has_shift  = "Starting"  in row.text or "Closing" in row.text
                if has_chiles and not has_shift:
                    continue  # purely Chiles, nothing to do
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

                try:
                    modal_el = driver.find_element(By.XPATH, '//div[@role="dialog"]')
                    remove_btns = modal_el.find_elements(By.CLASS_NAME, "c420")
                except Exception:
                    remove_btns = []

                if is_split:
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
                            except Exception:
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
                        _click_save_changes(wait)
                        time.sleep(1)
                    else:
                        _try_cancel(driver)
                    continue

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
                    _click_save_changes(wait)
                else:
                    item_name = row.text.split("\n")[0].strip()
                    if starting_opts:
                        kit_removals.append((item_name, "Closing Shift Mobile Inventory"))
                    elif closing_opts:
                        kit_removals.append((item_name, "Starting Shift Mobile Inventory"))
                    click(wait, (By.XPATH, _CANCEL_XPATH))

                time.sleep(1)

            except Exception as e:
                logger.error("pull_from_chiles: row %d failed: %s", idx, e)
                _try_cancel(driver)

        if kit_removals:
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
                        except Exception:
                            continue
                        remove_btn.click()
                        time.sleep(1)
                    except Exception as e:
                        logger.error("pull_from_chiles: kit removal failed for %s from %s: %s", product_name, kit_name, e)
            except Exception as e:
                logger.error("pull_from_chiles: kit removal navigation failed: %s", e)

    except Exception as e:
        logger.error("pull_from_chiles: row loop failed: %s", e)


@_webjoint_session
def dispatch_to_driver(driver, wait):
    try:
        _open_newest_order(wait)

        driver_button = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/div/div[2]/div/div/div[2]/div/div/div[2]/table/tbody/tr/td[1]/div/label/span[1]/span[1]/input')))
        scroll_into_view(driver, driver_button)
        driver_button.click()
        time.sleep(1)

        click(wait, (By.ID, 'selectActionButton'))
        time.sleep(1)

        click(wait, (By.ID, 'assignDriver'))
        time.sleep(2)

        confirm_button = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]')))
        time.sleep(1.5)
        confirm_button.click()
        time.sleep(1)

    except Exception as e:
        logger.error("dispatch_to_driver: failed to click newest order: %s", e)


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
    except Exception:
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
        except Exception:
            continue
        ratio = difflib.SequenceMatcher(None, target, _normalize_product_name(candidate)).ratio()
        if ratio > best_ratio:
            best_match, best_ratio = candidate, ratio

    time.sleep(0.5)

    return best_match if best_ratio >= _PRODUCT_NAME_MATCH_THRESHOLD else None


@_webjoint_session
def create_product(driver, wait, product_dicts: List[Dict[str, str]]):
    try:
        _open_inventory_page(wait, "Products")
        time.sleep(1)
    except Exception as e:
        logger.error("create_product: navigation failed: %s", e)
        return

    for product_dict in product_dicts:
        try:
            # fuzzy duplicate check disabled for now (helpers above still
            # intact — uncomment to re-enable):
            # existing = _find_existing_product(driver, wait, product_dict["product_name"])
            # if existing:
            #     logger.info(
            #         "create_product: skipping %r — fuzzy-matches existing product %r",
            #         product_dict["product_name"], existing,
            #     )
            #     continue

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

            time.sleep(1)

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
            click_with_js_fallback(driver, save_product_button)

            time.sleep(2)

        except Exception as e:
            logger.exception("create_product: product creation failed: %s", e)
            continue


@_webjoint_session
def set_webhooks(driver, wait, base_url: str) -> None:
    new_order_url = base_url + "/new_order"
    complete_order_url = base_url + "/complete_order"
    cancel_order_url = base_url + "/cancel_order"

    try:
        click(wait, (By.XPATH, '//*[@id="nav_drawer"]/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[9]'))
        click(wait, (By.XPATH, '//*[@id="nav_drawer"]/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[9]/div[2]/div/div/menu/a[1]'))
        click(wait, (By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div/div[6]/div[1]'))
    except Exception as e:
        logger.error("set_webhooks: navigation to order settings failed: %s", e)
        return

    try:
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
        click_with_js_fallback(driver, save_changes_button)
        logger.info("set_webhooks: saved — new_order=%s complete_order=%s", new_order_url, complete_order_url)
    except Exception as e:
        logger.error("set_webhooks: failed to set or save webhook URLs: %s", e)

    time.sleep(5)


@_webjoint_session
def get_customer_info(driver, wait):
    try:
        click(wait, (By.XPATH, _ORDERS_NAV_XPATH))
        time.sleep(1)

        click(wait, (By.XPATH, '/html/body/div[1]/div/div/main/div[2]/div/div[1]/div[2]/table/tbody/tr[1]/td[6]/span/a/div/span'))
        time.sleep(1)

        click(wait, (By.XPATH, "/html/body/div[1]/div/div/main/div[2]/div/div/form/div[1]/div/div/div/a[3]/span[1]/span/span"))
        time.sleep(1)

        order_count_text = wait.until(EC.element_to_be_clickable((By.XPATH, "/html/body/div[1]/div/div/main/div[2]/div/div/form/div[2]/span[3]/div/div/div/span/div/span[2]")))
        order_count = order_count_text.text.split()[2]
        time.sleep(1)

        click(wait, (By.XPATH, "/html/body/div[1]/div/div/main/div[2]/div/div/form/div[1]/div/div/div/a[4]/span[1]/span/span"))
        time.sleep(1)

        #check

        time.sleep(3)

    except Exception as e:
        logger.error("get_customer_info: failed to read customer info: %s", e)


if __name__ == "__main__":
    import sys
    import django
    from dotenv import load_dotenv
    load_dotenv()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TGPFflows.settings')
    django.setup()
    #pull_from_chiles()
    #dispatch_to_driver()
    #get_customer_info()

    test_list = [
        {
            "product_name": "Tropical Punch | 510 Cartridge",
            "brand": "Chico Verde",
            "product_type": "Concentrate",
            "lineage": "Indica",
            "strain": "Tropical Punch",
            "category": "Cartridges",
            "subcategory": "Value | 510 Thread",
            "seo_title": "Tropical Punch 510 Chico Verde",
            "description": "Sweet, tropical fruit punch meets a relaxing indica body effect — smooth, flavorful, and perfect for easing into a calm, mellow evening.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 1,
            "unit": "g",
            "name_variant": "Tropical Punch 510",
            "price": 13.99,
            "cannabinoids": {}
        },
        {
            "product_name": "Blue Dot | 1g Preroll",
            "brand": "Quiet Kings",
            "product_type": "Buds",
            "lineage": "Sativa",
            "strain": "Blue Dot",
            "category": "Prerolls",
            "subcategory": "House Preroll",
            "seo_title": "Blue Dot 1g Preroll Quiet Kings",
            "description": "Bright and energizing, Blue Dot delivers a clear-headed sativa lift with smooth, lightly sweet flavor — an easy go-to for staying active and focused.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 1,
            "unit": "g",
            "name_variant": "Blue Dot",
            "price": 3.39,
            "cannabinoids": {}
        },
        {
            "product_name": "Dream Queen | 14 Pack | 0.5g per | 7g total",
            "brand": "Quiet Kings",
            "product_type": "Buds",
            "lineage": "Hybrid",
            "strain": "Dream Queen",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "Dream Queen 14 Pack Quiet Kings",
            "description": "A dreamy, well-balanced hybrid that offers smooth relaxation with a gentle uplift — Dream Queen is easygoing and consistent across the full 14-pack.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 7,
            "unit": "g",
            "name_variant": "Dream Queen",
            "price": 18.99,
            "cannabinoids": {}
        }
    ]
    create_product(test_list)
    