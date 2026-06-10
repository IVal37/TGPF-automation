# imports from std lib
import logging
import os
import time

logger = logging.getLogger(__name__)

# imports from django
# from django.conf import settings

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
    # if not settings.TEST_MODE:
    #     opts.add_argument("--headless=new")
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

                    if len(remove_btns) >= 2:
                        chiles_idx = None
                        shift_idx = None
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
                        cancel_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[text()="CANCEL"]')))
                        cancel_btn.click()

                    time.sleep(1)

                except Exception as e:
                    logger.error("pull_from_chiles: row %d failed: %s", idx, e)
                    try:
                        driver.find_element(By.XPATH, '//*[text()="CANCEL"]').click()
                    except Exception:
                        pass

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

            action_button = wait.until(EC.element_to_be_clickable((By.ID, 'selectActionButton')))
            action_button.click()

            dispatch_button = wait.until(EC.element_to_be_clickable((By.ID, 'assignDriver')))
            dispatch_button.click()

            time.sleep(1)

        except Exception as e:
            logger.error("dispatch_to_driver: failed to click newest order: %s", e)

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
    pull_from_chiles()
    #dispatch_to_driver()
