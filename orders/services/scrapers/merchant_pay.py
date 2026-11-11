# imports from std lib
import logging
import time

logger = logging.getLogger(__name__)

# imports from django
from django.conf import settings

# imports from project
from orders.services.scrapers.browser import browser_session, click, fill, scroll_into_view
from orders.services.scrapers.merchant_pay_helper import FetchCode as GetCode

# imports from selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

def send_merchant_pay_link():
    with browser_session("send_merchant_pay_link") as (driver, wait):
        driver.get("https://merchantpay.greenbaxmarketplace.io/login")

        try:          
            fill(wait, (By.ID, "mat-input-0"), settings.MERCHANT_PAY_EMAIL)
            fill(wait, (By.ID, "mat-input-1"), settings.MERCHANT_PAY_PASSWORD)
            click(wait, (By.XPATH, "/html/body/app-root/app-login-page/app-login/app-not-auth/div/section/form/button"))
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
                scroll_into_view(driver, code_input)
                code_input.clear()
                code_input.send_keys(code)
                time.sleep(0.5)

                click(wait, (By.XPATH, "//button[@type='submit' and normalize-space()='Verify']"))
            except Exception as e:
                logger.error("send_merchant_pay_link: 2FA verification step failed: %s", e)

        time.sleep(5)

