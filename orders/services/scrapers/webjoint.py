# imports from std lib
import difflib
import logging
import os
import re
import time
from typing import Dict, Optional, Tuple, List

logger = logging.getLogger(__name__)

# imports from django
from django.conf import settings

# imports from selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

BLOCK = 2
_WEBJOINT_URL = "https://order.thegoodpeoplefarms.com/admin/index.html#/"


def _create_driver():
    prefs = {"profile.default_content_setting_values.notifications": BLOCK}
    opts = Options()
    opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--window-size=1280,720")
    if settings.HEADLESS:
        opts.add_argument("--headless=new")
    driver = webdriver.Chrome(options=opts)
    return driver, WebDriverWait(driver, 15)


def _login(driver, wait):
    driver.get(_WEBJOINT_URL)
    email_input = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "c199.c13")))
    email_input.clear()
    email_input.send_keys(os.environ.get('WEBJOINT_EMAIL', ''))
    password_input = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "c199.c202.c13")))
    password_input.clear()
    password_input.send_keys(os.environ.get('WEBJOINT_PASSWORD', ''))
    login_button = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "c230.c204.c206.c209.c15")))
    login_button.click()


def fill_order_notes(order_notes: str):
    driver, wait = _create_driver()
    try:
        try:
            _login(driver, wait)
        except Exception as e:
            logger.error("fill_order_notes: login failed: %s", e)
            return

        try:
            orders_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[@id='nav_drawer']/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[2]/a")))
            orders_button.click()

            newest_order = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div[1]/div[2]/table/tbody/tr[1]/td[2]/span')))
            newest_order.click()

            # button is a child of the <p> containing "Order Notes" text
            order_notes_button = wait.until(EC.element_to_be_clickable(
                (By.XPATH, '//p[contains(translate(text(),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"order notes")]/button')
            ))
            order_notes_button.click()

            order_notes_input = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[4]/div[2]/div[2]/form/div[1]/div/div/div/div/div/textarea[3]')))
            existing = order_notes_input.get_attribute('value') or ''
            order_notes_input.clear()
            order_notes_input.send_keys(order_notes + ('\n' + existing if existing else ''))

            save_notes_button = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[4]/div[2]/div[2]/form/div[2]/button')))
            save_notes_button.click()

            time.sleep(2)

        except Exception as e:
            logger.error("fill_order_notes: navigation/notes step failed: %s", e)
    finally:
        driver.quit()


def pull_from_chiles():
    driver, wait = _create_driver()
    try:
        try:
            _login(driver, wait)
        except Exception as e:
            logger.error("pull_from_chiles: login failed: %s", e)
            return

        try:
            orders_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[@id='nav_drawer']/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[2]/a")))
            orders_button.click()

            newest_order = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div[1]/div[2]/table/tbody/tr[1]/td[2]/span')))
            newest_order.click()
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
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", edit_btn)
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
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pulling_from_button)
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
                            time.sleep(0.5)
                            split_save_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[contains(.,"Save Changes")]')))
                            split_save_btn.click()
                            time.sleep(1)
                        else:
                            try:
                                driver.find_element(By.XPATH, '//*[text()="Cancel"]').click()
                            except Exception:
                                pass
                        continue

                    # css- classes from emotion are stable (hashed from styles, not random)
                    package_select_control = wait.until(EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, 'div.css-1j4kz5m')
                    ))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", package_select_control)
                    time.sleep(0.3)
                    package_select_control.click()
                    time.sleep(1)

                    chiles_opts   = driver.find_elements(By.XPATH, '//*[@role="option" and contains(.,"@ Chiles Rd Shop")]')
                    closing_opts  = driver.find_elements(By.XPATH, '//*[@role="option" and contains(.,"@ Closing Shift Mobile Inventory")]')
                    starting_opts = driver.find_elements(By.XPATH, '//*[@role="option" and contains(.,"@ Starting Shift Mobile Inventory")]')

                    time.sleep(1)

                    if chiles_opts:
                        chiles_opts[0].click()
                        time.sleep(0.5)
                        save_btn = wait.until(EC.element_to_be_clickable(
                            (By.XPATH, '//button[contains(.,"Save Changes")]')
                        ))
                        save_btn.click()
                    else:
                        item_name = row.text.split("\n")[0].strip()
                        if starting_opts:
                            kit_removals.append((item_name, "Closing Shift Mobile Inventory"))
                        elif closing_opts:
                            kit_removals.append((item_name, "Starting Shift Mobile Inventory"))
                        cancel_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="Cancel"]')))
                        cancel_btn.click()

                    time.sleep(1)

                except Exception as e:
                    logger.error("pull_from_chiles: row %d failed: %s", idx, e)
                    try:
                        driver.find_element(By.XPATH, '//*[text()="Cancel"]').click()
                    except Exception:
                        pass

            if kit_removals:
                try:
                    inventory_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="Inventory"]')))
                    inventory_button.click()
                    kits_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="Kits"]')))
                    kits_button.click()
                    for product_name, kit_name in kit_removals:
                        try:
                            kit_button = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[text()="{kit_name}"]')))
                            kit_button.click()
                            time.sleep(1)
                            search_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span/div/div[2]/div/div/div/input')))
                            search_input.clear()
                            search_input.send_keys(product_name)
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
    finally:
        driver.quit()

def dispatch_to_driver():
    driver, wait = _create_driver()
    try:
        try:
            _login(driver, wait)
        except Exception as e:
            logger.error("dispatch_to_driver: login failed: %s", e)
            return

        try:
            orders_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[@id='nav_drawer']/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[2]/a")))
            orders_button.click()

            newest_order = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div[1]/div[2]/table/tbody/tr[1]/td[2]/span')))
            newest_order.click()

            driver_button = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/div/div[2]/div/div/div[2]/div/div/div[2]/table/tbody/tr/td[1]/div/label/span[1]/span[1]/input')))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", driver_button)
            driver_button.click()
            time.sleep(1)

            action_button = wait.until(EC.element_to_be_clickable((By.ID, 'selectActionButton')))
            action_button.click()
            time.sleep(1)

            dispatch_button = wait.until(EC.element_to_be_clickable((By.ID, 'assignDriver')))
            dispatch_button.click()
            time.sleep(1)

            confirm_button = wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]')))
            confirm_button.click()
            time.sleep(2)

        except Exception as e:
            logger.error("dispatch_to_driver: failed to click newest order: %s", e)

    finally:
        driver.quit()


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

    exact = next((o for o in options if o.text.strip() == value), None)
    if exact:
        exact.click()
        return

    create_opt = next((o for o in options if o.text.strip() == f'Create "{value}"'), None)
    if create_opt:
        create_opt.click()
        return

    input_el.send_keys(Keys.ENTER)


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


def _find_existing_product(driver, wait, name: str) -> Optional[str]:
    """Search the Products list for an existing product matching `name`.

    Webjoint's saved title often varies from the invoice-parsed name (added
    weight/type words, different delimiters), so this fuzzy-matches on the
    normalized text of the search results rather than requiring an exact hit.
    Returns the matching product's name as displayed in webjoint, or None.
    """
    search_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//input[@placeholder="Search Products..."]')))
    query = name.split("|")[0].strip()
    search_input.clear()
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

    search_input.clear()
    time.sleep(0.5)

    return best_match if best_ratio >= _PRODUCT_NAME_MATCH_THRESHOLD else None


def create_product(product_dicts: List[Dict[str, str]]):
    driver, wait = _create_driver()
    try:
        try:
            _login(driver, wait)
        except Exception as e:
            logger.error("create_product: login failed: %s", e)
            return

        try:
            inventory_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="Inventory"]')))
            inventory_button.click()

            products_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="Products"]')))
            products_button.click()

            time.sleep(1)
        except Exception as e:
            logger.error("create_product: navigation failed: %s", e)
            return
        
        for product_dict in product_dicts:
            try:
                existing = _find_existing_product(driver, wait, product_dict["product_name"])
                if existing:
                    logger.info(
                        "create_product: skipping %r — fuzzy-matches existing product %r",
                        product_dict["product_name"], existing,
                    )
                    continue

                add_product_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="Add a New Product"]')))
                add_product_button.click()
                time.sleep(0.5)

                name_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="name"]')))
                name_input.clear()
                name_input.send_keys(product_dict["product_name"])
                time.sleep(0.5)

                brand_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="brand"]//input')))
                _fill_autocomplete(driver, wait, brand_input, product_dict["brand"])
                time.sleep(0.5)

                product_type_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="product_type"]//input')))
                _fill_autocomplete(driver, wait, product_type_input, product_dict["product_type"])
                time.sleep(0.5)

                lineage_dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="lineage"]/div/div[1]/div[2]')))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", lineage_dropdown)
                lineage_dropdown.click()
                time.sleep(0.5)
                lineage_option = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@role="option" and text()="{product_dict["lineage"]}"]')))
                lineage_option.click()
                time.sleep(0.5)

                strain_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="strain"]//input')))
                _fill_autocomplete(driver, wait, strain_input, product_dict["strain"])
                time.sleep(0.5)

                category_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="category"]//input')))
                _fill_autocomplete(driver, wait, category_input, product_dict["category"])
                time.sleep(0.5)

                subcategory_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="subcategory"]//input')))
                _fill_autocomplete(driver, wait, subcategory_input, product_dict["subcategory"])
                time.sleep(0.5)

                if product_dict.get("for_sale", "") == "No":
                    not_for_sale_label = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="forSale"]//label[2]')))
                    not_for_sale_label.click()
                    time.sleep(0.5)

                discount_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="salepricediscount"]')))
                discount_input.clear()
                discount_input.send_keys(product_dict["sale_discount"])
                time.sleep(0.5)

                seo_title_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="seoTitle"]')))
                seo_title_input.clear()
                seo_title_input.send_keys(product_dict["seo_title"])
                time.sleep(0.5)

                description_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span[1]/div/div[2]/div[8]/div/div/div/div[3]/div[1]')))
                description_input.clear()
                description_input.send_keys(product_dict["description"])
                time.sleep(0.5)

                seo_description_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="seo_description"]')))
                seo_description_input.clear()
                seo_description_input.send_keys(product_dict["description"])
                time.sleep(0.5)

                pricing_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[1]/div/div/div/a[2]/span[1]/span/span')))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pricing_button)
                time.sleep(0.3)
                pricing_button.click()
                time.sleep(0.5)

                add_price_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span[2]/div/div/ul/li/span/button/span[1]')))
                add_price_button.click()
                time.sleep(0.5)

                weight_size_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="variants[0].unitweight"]')))
                weight_size_input.clear()
                weight_size_input.send_keys(product_dict["weight_size"])
                time.sleep(0.5)

                unit_dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="unit"]/div/div[1]/div[2]')))
                unit_dropdown.click()
                unit_option = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@role="option" and text()="{product_dict["unit"]}"]')))
                unit_option.click()
                time.sleep(0.5)

                price_input = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="variants[0].price"]')))
                price_input.clear()
                price_input.send_keys(product_dict["price"])
                time.sleep(0.5)

                cannabinoids_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[1]/div/div/div/a[3]/span[1]/span/span')))
                cannabinoids_button.click()
                time.sleep(0.5)

                for idx, (cannabinoid, value) in enumerate(product_dict.get("cannabinoids", {}).items()):
                    amount, unit_option = _parse_cannabinoid_value(value)

                    add_cannabinoid_button = wait.until(EC.element_to_be_clickable((By.XPATH,
                        '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[2]/span[3]'
                        '//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "add")]'
                    )))
                    add_cannabinoid_button.click()
                    time.sleep(0.5)

                    # each added row gets its own (non-unique) id="type"/"id="unit"; the
                    # most recently added row is always the last match in the DOM
                    cannabinoid_type_dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, '(//*[@id="type"])[last()]/div/div[1]/div[2]')))
                    cannabinoid_type_dropdown.click()
                    cannabinoid_type_option = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@role="option" and text()="{cannabinoid}"]')))
                    cannabinoid_type_option.click()
                    time.sleep(0.5)

                    cannabinoid_unit_dropdown = wait.until(EC.element_to_be_clickable(
                        (By.XPATH, '(//*[@id="unit"])[last()]//div[contains(@class, "indicatorContainer")]')
                    ))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cannabinoid_unit_dropdown)
                    time.sleep(0.3)
                    cannabinoid_unit_dropdown.click()
                    cannabinoid_unit_option = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@role="option" and text()="{unit_option}"]')))
                    cannabinoid_unit_option.click()
                    time.sleep(0.5)

                    min_input = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@id="cannabinoids[{idx}].min"]')))
                    min_input.clear()
                    min_input.send_keys(amount)
                    time.sleep(0.5)

                    max_input = wait.until(EC.element_to_be_clickable((By.XPATH, f'//*[@id="cannabinoids[{idx}].max"]')))
                    max_input.clear()
                    max_input.send_keys(amount)
                    time.sleep(0.5)

                save_product_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div[3]/button/span[1]')))
                save_product_button.click()
                
                time.sleep(2)

            except Exception as e:
                logger.error("create_product: product creation failed: %s", e)
                continue

    finally:
        driver.quit()

def set_webhooks(base_url: str) -> None:
    new_order_url = base_url + "/new_order"
    complete_order_url = base_url + "/complete_order"
    cancel_order_url = base_url + "/cancel_order"

    driver, wait = _create_driver()
    try:
        try:
            _login(driver, wait)
        except Exception as e:
            logger.error("set_webhooks: login failed: %s", e)
            return

        try:
            settings_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="nav_drawer"]/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[9]')))
            settings_button.click()

            general_settings_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="nav_drawer"]/div/div[2]/div/div[1]/div[2]/div/div/div/nav/div[9]/div[2]/div/div/menu/a[1]')))
            general_settings_button.click()

            order_settings_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div/div[6]/div[1]')))
            order_settings_button.click()
        except Exception as e:
            logger.error("set_webhooks: navigation to order settings failed: %s", e)
            return

        try:
            order_submit_hook_input = wait.until(EC.element_to_be_clickable((By.ID, "hooks.orderSubmit")))
            order_submit_hook_input.clear()
            order_submit_hook_input.send_keys(new_order_url)

            order_complete_hook_input = wait.until(EC.element_to_be_clickable((By.ID, "hooks.orderComplete")))
            order_complete_hook_input.clear()
            order_complete_hook_input.send_keys(complete_order_url)

            order_cancel_hook_input = wait.until(EC.element_to_be_clickable((By.ID, "hooks.orderCancel")))
            order_cancel_hook_input.clear()
            order_cancel_hook_input.send_keys(cancel_order_url)

            save_changes_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/main/div[2]/div/div/form/div/div[16]/div[1]/button')))
            save_changes_button.click()
            logger.info("set_webhooks: saved — new_order=%s complete_order=%s", new_order_url, complete_order_url)
        except Exception as e:
            logger.error("set_webhooks: failed to set or save webhook URLs: %s", e)

        time.sleep(5)

    finally:
        driver.quit()


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

    test_list = [
        {
            "product_name": "OG Kush x Northern Lights | Junior | 0.75g 4-Pack Preroll",
            "brand": "Kingroll",
            "product_type": "Buds",
            "lineage": "Indica",
            "strain": "OG Kush x Northern Lights",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "OG Kush x Northern Lights Junior 4-Pack Kingroll",
            "description": "Two indica legends united — OG Kush x Northern Lights brings classic earthy pine and sweet musk together in a deeply relaxing cross that settles the body and quiets the mind. This Junior 4-pack is built for easy, consistent evening sessions.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 3,
            "unit": "g",
            "name_variant": "OG Kush x Northern Lights",
            "price": 36.99
        },
        {
            "product_name": "Berries and Cream | 510 Threaded",
            "brand": "Dime Bag",
            "product_type": "Concentrate",
            "lineage": "Hybrid",
            "strain": "Berries and Cream",
            "category": "Cartridges",
            "subcategory": "Mid Shelf | 510",
            "seo_title": "Berries and Cream 510 Dime Bag",
            "description": "Sweet, smooth, and satisfying — Berries and Cream delivers a lush mix of ripe berry flavor with a creamy, dessert-like finish. This hybrid 510 cart offers a balanced, easygoing high that's perfect for unwinding without fully checking out.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 1,
            "unit": "g",
            "name_variant": "Berries and Cream 510",
            "price": 18.99
        },
        {
            "product_name": "Mango Lemonade | 510 Threaded",
            "brand": "Dime Bag",
            "product_type": "Concentrate",
            "lineage": "Sativa",
            "strain": "Mango Lemonade",
            "category": "Cartridges",
            "subcategory": "Mid Shelf | 510",
            "seo_title": "Mango Lemonade 510 Dime Bag",
            "description": "Bright, tropical, and refreshing — Mango Lemonade blends ripe mango sweetness with a sharp citrus finish for a sativa cart that's as uplifting as it is flavorful. A go-to for daytime energy and a sunny mood boost.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 1,
            "unit": "g",
            "name_variant": "Mango Lemonade 510",
            "price": 18.99
        },
        {
            "product_name": "Lemon Diesel | FZL Originals | 2.5g Preroll",
            "brand": "Fuzzies",
            "product_type": "Buds",
            "lineage": "Sativa",
            "strain": "Lemon Diesel",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "Lemon Diesel Preroll Fuzzies",
            "description": "Sharp citrus and fuel collide in Lemon Diesel — a punchy sativa-dominant strain with a bright, zesty aroma and an energizing, clear-headed buzz. Perfect for daytime productivity or a creative boost.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 2.5,
            "unit": "g",
            "name_variant": "Lemon Diesel",
            "price": 24.99
        },
        {
            "product_name": "Magic Melon x Bananalope Haze | Junior | 0.75g 4-Pack Preroll",
            "brand": "Kingroll",
            "product_type": "Buds",
            "lineage": "Sativa",
            "strain": "Magic Melon x Bananalope Haze",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "Magic Melon x Bananalope Haze Junior 4-Pack Kingroll",
            "description": "Sweet cantaloupe and tropical banana haze come together in a bright, uplifting sativa cross that keeps energy high and the mood light. This Junior 4-pack is a fruity, fun option for daytime sharing.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 3,
            "unit": "g",
            "name_variant": "Magic Melon x Bananalope Haze",
            "price": 36.99
        },
        {
            "product_name": "Cherry Limeade | 510 Threaded",
            "brand": "Dime Bag",
            "product_type": "Concentrate",
            "lineage": "Hybrid",
            "strain": "Cherry Limeade",
            "category": "Cartridges",
            "subcategory": "Mid Shelf | 510",
            "seo_title": "Cherry Limeade 510 Dime Bag",
            "description": "Tart cherry and zesty lime make Cherry Limeade one of the most refreshing carts in the lineup — a hybrid that balances sweet fruit flavor with a clean, balanced high that works any time of day.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 1,
            "unit": "g",
            "name_variant": "Cherry Limeade 510",
            "price": 18.99
        },
        {
            "product_name": "Grape Ape | FZL Originals | 2.5g Preroll",
            "brand": "Fuzzies",
            "product_type": "Buds",
            "lineage": "Indica",
            "strain": "Grape Ape",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "Grape Ape Preroll Fuzzies",
            "description": "Sweet purple grape and berry aromatics lead into a smooth, deeply relaxing indica experience — Grape Ape is a classic strain that delivers consistent full-body ease and a calm, settled headspace. A reliable evening preroll.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 2.5,
            "unit": "g",
            "name_variant": "Grape Ape",
            "price": 24.99
        },
        {
            "product_name": "Blue Dream | FZL Originals | 2.5g Preroll",
            "brand": "Fuzzies",
            "product_type": "Buds",
            "lineage": "Hybrid",
            "strain": "Blue Dream",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "Blue Dream Preroll Fuzzies",
            "description": "Bright and balanced, Blue Dream is a California classic that pairs sweet berry aromatics with a smooth, uplifting cerebral high. This sativa-leaning hybrid is easygoing enough for daytime use, delivering creative energy and full-body relaxation without sedation.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 2.5,
            "unit": "g",
            "name_variant": "Blue Dream",
            "price": 24.99
        },
        {
            "product_name": "White Rntz x Apple Fritter | Junior | 0.75g 4-Pack Preroll",
            "brand": "Kingroll",
            "product_type": "Buds",
            "lineage": "Hybrid",
            "strain": "White Rntz x Apple Fritter",
            "category": "Prerolls",
            "subcategory": "Non-Infused Preroll Packs",
            "seo_title": "White Rntz x Apple Fritter Junior 4-Pack Kingroll",
            "description": "Candy-sweet Runtz meets warm, doughy Apple Fritter in a hybrid cross that's rich in flavor and balanced in effect. Euphoric and relaxing in equal measure, this Junior 4-pack is a dessert lover's dream spread across four smooth sessions.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 3,
            "unit": "g",
            "name_variant": "White Rntz x Apple Fritter",
            "price": 36.99
        }
    ]

    create_product(test_list)
