# imports from std lib
import os
import time

# imports from selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# imports from project
from orders.services.scrapers.browser import click, create_driver, fill

def get_wm_payment_type():
    driver, wait = create_driver()

    payment_type = "Cash"

    try:
        driver.get("https://weedmaps.com/login?return_url=https://admin.weedmaps.com/orders")

        # intial login
        try:          
            fill(wait, (By.ID, "user_username"), os.environ.get('WEEDMAPS_EMAIL', ''))
            fill(wait, (By.ID, "user_password"), os.environ.get('WEEDMAPS_PASSWORD', ''))
            click(wait, (By.CLASS_NAME, "btn.btn-signup-primary"))
        except Exception:
            pass

        # move to orders
        try:          
            click(wait, (By.ID, "side-nav-orders-all-orders"))
        except Exception:
            pass

        # click into most recent order
        try:
            click(wait, (By.XPATH, '//*[@id="__next"]/div/div[2]/div/div[3]/div[2]/div[1]/div[2]/div[1]/div[1]/span/a'))
        except Exception:
            pass

        # first-time orders show a verification screen that hides payment info —
        # click through both prep buttons if present before reading payment type
        short_wait = WebDriverWait(driver, 4)
        try:
            confirm_btn = short_wait.until(EC.element_to_be_clickable(
                (By.XPATH, '//button[contains(., "Confirm & start preparing")]')
            ))
            confirm_btn.click()
            time.sleep(2)
        except Exception:
            pass

        # get payment type
        try:
            payment_type_field = wait.until(EC.presence_of_element_located((By.XPATH , '//*[@id="__next"]/div/div[2]/div/div[3]/div[3]/div[1]/div[1]/div/div/div[4]/div[1]/p[1]')))
            payment_type = payment_type_field.text

        except Exception:
            pass

        time.sleep(5)

    finally:
        driver.quit()

    return regularize_type(payment_type)

def regularize_type(wm_type: str) -> str:
    if wm_type == "Cashless":
        return "Debit / Tap-to-pay"
    else:
        return "Cash"

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    get_wm_payment_type()