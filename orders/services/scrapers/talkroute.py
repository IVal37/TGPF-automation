# imports from std lib
import os
import time

# imports from selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

# imports from project
from orders.services.scrapers.browser import BLOCK, click, create_driver, fill

def send_message(customer_number: str, dispatch_msg: str):
    driver, wait = create_driver({"profile.default_content_setting_values.media_stream_mic": BLOCK})

    try:
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
        try:
            click(wait, (By.ID, "navigation-messages-item"))
            click(wait, (By.XPATH, '//*[@id="root"]/div/div/div[2]/div/div[1]/div/div[1]/div/div[1]/button'))
            fill(wait, (By.ID, "conversations-to-phone-number-input"), customer_number)
            fill(wait, (By.CLASS_NAME, "message-input.text-message-input"), dispatch_msg)
            click(wait, (By.CLASS_NAME, "message-input-send"))
            
            time.sleep(5)
        except Exception:
            pass       

    finally:
        driver.quit()

def send_testing_message():
    send_message(os.environ.get('TALKROUTE_TEST_PHONE', ''), "testing")

if __name__ == "__main__":
    import sys
    import django
    from dotenv import load_dotenv
    load_dotenv()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "TGPFflows.settings")
    django.setup()
    send_testing_message()
