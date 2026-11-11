# imports from std lib
import logging
import time

# imports from django
from django.conf import settings

# imports from selenium
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# imports from project
from orders.constants import PaymentType
from orders.services.scrapers.browser import browser_session, click, fill
from orders.services.scrapers.errors import BeforeCommitError

logger = logging.getLogger(__name__)


# weedmaps_order_id is the number Weedmaps shows as "#49018286"; WebJoint's
# webhook carries it as "weedmapsOrderId". We open exactly that order, and only
# read a payment type once its page confirms it is that order.
#
# This scraper is read-only (the "Confirm & start preparing" click only starts
# prep and stays as it is), so there is no commit point: any failure to find
# the order or read its payment type raises BeforeCommitError and the task
# retries. Without a Weedmaps number there is nothing to look up and it returns
# "Cash" with no browser. The order stays "Payment: checking..." on the
# dashboard after a final failure, where "Send as cash" is still available.
def get_wm_payment_type(weedmaps_order_id):
    payment_type = PaymentType.CASH

    if not weedmaps_order_id:
        logger.warning("get_wm_payment_type: no Weedmaps order id in the webhook; defaulting to Cash")
        return regularize_type(payment_type)

    failure = None
    with browser_session("get_wm_payment_type", f"wm{weedmaps_order_id}") as (driver, wait):
        driver.get("https://weedmaps.com/login?return_url=https://admin.weedmaps.com/orders")

        # intial login
        try:
            fill(wait, (By.ID, "user_username"), settings.WEEDMAPS_EMAIL)
            fill(wait, (By.ID, "user_password"), settings.WEEDMAPS_PASSWORD)
            click(wait, (By.CLASS_NAME, "btn.btn-signup-primary"))
        except WebDriverException:
            pass

        # move to orders
        try:
            click(wait, (By.ID, "side-nav-orders-all-orders"))
        except WebDriverException:
            pass

        # click into this order's row, then check the page header says it is
        # this order before touching anything else
        order_opened = False
        try:
            click(wait, (By.XPATH, f'//a[starts-with(@href, "/orders/") and normalize-space(.)="#{weedmaps_order_id}"]'))
            wait.until(EC.presence_of_element_located(
                (By.XPATH, f'//span[normalize-space(.)="Order #{weedmaps_order_id}"]')
            ))
            order_opened = True
        except Exception as e:
            logger.warning(
                "get_wm_payment_type (Weedmaps order %s): order not found or page is another order (%s)",
                weedmaps_order_id, type(e).__name__,
            )
            failure = BeforeCommitError("open Weedmaps order", e)

        if order_opened:
            # first-time orders show a verification screen that hides payment info —
            # click through both prep buttons if present before reading payment type
            short_wait = WebDriverWait(driver, 4)
            try:
                confirm_btn = short_wait.until(EC.element_to_be_clickable(
                    (By.XPATH, '//button[contains(., "Confirm & start preparing")]')
                ))
                confirm_btn.click()
                time.sleep(2)
            except WebDriverException:
                pass

            # get payment type
            try:
                payment_type_field = wait.until(EC.presence_of_element_located((By.XPATH , '//*[@id="__next"]/div/div[2]/div/div[3]/div[3]/div[1]/div[1]/div/div/div[4]/div[1]/p[1]')))
                payment_type = payment_type_field.text
            except Exception as e:
                failure = BeforeCommitError("read payment type", e)

        # the wait after the lookup is kept whether or not it worked, as before
        time.sleep(5)

        if failure is not None:
            raise failure

    return regularize_type(payment_type)

def regularize_type(wm_type: str) -> str:
    if wm_type == "Cashless":
        return PaymentType.DEBIT
    else:
        return PaymentType.CASH

