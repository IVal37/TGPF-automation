# imports from std lib
import logging
import os
import time

# imports from django
from django.conf import settings

logger = logging.getLogger(__name__)

# imports from project
from orders.services.scrapers.merchant_pay_helper import FetchCode as GetCode

# imports from selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

BLOCK = 2

def send_merchant_pay_link():
    opts = Options()
    opts.add_argument("--window-size=1280,720")
    if settings.HEADLESS:
        opts.add_argument("--headless=new")

    driver = webdriver.Chrome(options=opts)
    wait = WebDriverWait(driver, 15)

    try:
        driver.get("https://merchantpay.greenbaxmarketplace.io/login")

        try:          
            email = wait.until(EC.element_to_be_clickable((By.ID, "mat-input-0")))
            email.clear()
            email.send_keys(os.environ.get('MERCHANT_PAY_EMAIL', ''))

            password = wait.until(EC.element_to_be_clickable((By.ID, "mat-input-1")))
            password.clear()
            password.send_keys(os.environ.get('MERCHANT_PAY_PASSWORD', ''))

            login_button = wait.until(EC.element_to_be_clickable((By.XPATH, "/html/body/app-root/app-login-page/app-login/app-not-auth/div/section/form/button")))
            login_button.click()
        except Exception:
            pass

        try:
            code = GetCode()
        except Exception as e:
            logger.error("send_merchant_pay_link: fetching 2FA code failed: %s", e)
            code = None

        if code:
            try:
                locator = (By.XPATH, "//mat-form-field[.//mat-label[normalize-space()='Authentication code']]//input")
                code_input = wait.until(EC.element_to_be_clickable(locator))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'})", code_input)
                code_input.clear()
                code_input.send_keys(code)
                time.sleep(0.5)

                verify_button = wait.until(EC.element_to_be_clickable(
                    (By.XPATH, "//button[@type='submit' and normalize-space()='Verify']")
                ))
                verify_button.click()
            except Exception as e:
                logger.error("send_merchant_pay_link: 2FA verification step failed: %s", e)

        time.sleep(5)

    finally:
        driver.quit()

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    send_merchant_pay_link()