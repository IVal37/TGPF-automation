# imports from std lib
import os
import time

# imports from selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

# imports from project
from orders.services.scrapers.browser import BLOCK, browser_session, click, click_at_commit, fill
from orders.services.scrapers.errors import at_commit, before_commit

# The commit point is the click on "send" (message-input-send). Everything before
# it raises BeforeCommitError, so the task retries safely; a failure at or after
# it propagates as-is and is never retried automatically (the text may be out).
# The login and the first-time setup modals are optional and stay swallowed.
# order_ref names the order in failure logs/screenshots (never the phone number).
def send_message(customer_number: str, dispatch_msg: str, order_ref=None):
    with browser_session(
        "send_message", order_ref, {"profile.default_content_setting_values.media_stream_mic": BLOCK},
    ) as (driver, wait):
        driver.get("https://my.talkroute.com/home/text_messages/")

        # initial login
        try:
            fill(wait, (By.ID, "login-page-email-input"), os.environ.get('TALKROUTE_EMAIL', ''))
            fill(wait, (By.ID, "login-page-password-input"), os.environ.get('TALKROUTE_PASSWORD', ''))
            click(wait, (By.ID, "login-page-login-button"))
        except Exception:
            pass

        # get through setup menu
        try:
            click(wait, (By.CLASS_NAME, "ui.basic.icon.button.initialModalButton.modal-accept-button"))
            click(wait, (By.CLASS_NAME, "ui.basic.icon.button.initialModalButton.modal-accept-button"))
            click(wait, (By.CSS_SELECTOR, "label[for='modal-terms-checkbox']"))
            click(wait, (By.CLASS_NAME, "button.modal-accept-button:not(.initialModalButton)"))
            click(wait, (By.XPATH, ".//button[normalize-space()='OK']"))
        except Exception:
            pass

        # change number to working one
        '''
        try:
            settings_button = wait.until(EC.element_to_be_clickable((By.ID, "show-preferences-button")))
            settings_button.click()

            call_dropdown = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "MuiInputBase-root")))
            call_dropdown.click()

            call_num = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, f"li.MuiButtonBase-root.MuiListItem-root.MuiMenuItem-root.MuiMenuItem-gutters.MuiListItem-gutters.MuiListItem-button[data-value='{talkroute_phone}']")))
            call_num.click()
            
            text_dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="mui-component-select-OUTGOING_TEXT_PHONE_NUMBER_SELECTED"]')))
            text_dropdown.click()

            text_num = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="menu-OUTGOING_TEXT_PHONE_NUMBER_SELECTED"]/div[3]/ul/li[2]')))
            text_num.click()

            close_settings_button = wait.until(EC.element_to_be_clickable((By.ID, "close-preferences-button")))
            close_settings_button.click()
        except Exception:
            pass
        '''
        # send message
        send_locator = (By.CLASS_NAME, "message-input-send")
        with before_commit("compose message"):
            click(wait, (By.ID, "navigation-messages-item"))
            click(wait, (By.XPATH, '//*[@id="root"]/div/div/div[2]/div/div[1]/div/div[1]/div/div[1]/button'))
            fill(wait, (By.ID, "conversations-to-phone-number-input"), customer_number)
            fill(wait, (By.CLASS_NAME, "message-input.text-message-input"), dispatch_msg)
            send_button = wait.until(EC.element_to_be_clickable(send_locator))

        with at_commit("send message"):
            click_at_commit(wait, send_locator, send_button)

            time.sleep(5)

def send_testing_message():
    send_message(os.environ.get('TALKROUTE_TEST_PHONE', ''), "testing", order_ref="test")

if __name__ == "__main__":
    import sys
    import django
    from dotenv import load_dotenv
    load_dotenv()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "TGPFflows.settings")
    django.setup()
    send_testing_message()
