# imports from std lib
import os
import time

# imports from selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

BLOCK = 2

def send_message(customer_number: str, dispatch_msg: str):
    prefs = {
        "profile.default_content_setting_values.media_stream_mic": BLOCK
    }
    opts = Options()
    opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--window-size=1280,720")

    driver = webdriver.Chrome(options=opts)
    wait = WebDriverWait(driver, 15)

    try:
        driver.get("https://my.talkroute.com/home/text_messages/")

        # initial login
        try:
            email = wait.until(EC.element_to_be_clickable((By.ID, "login-page-email-input")))
            email.clear()
            email.send_keys(os.environ.get('TALKROUTE_EMAIL', ''))

            password = wait.until(EC.element_to_be_clickable((By.ID, "login-page-password-input")))
            password.clear()
            password.send_keys(os.environ.get('TALKROUTE_PASSWORD', ''))

            login_button = wait.until(EC.element_to_be_clickable((By.ID, "login-page-login-button")))
            login_button.click()
        except Exception:
            pass

        # get through setup menu
        try:
            next_button = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "ui.basic.icon.button.initialModalButton.modal-accept-button")))
            next_button.click()

            next_button = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "ui.basic.icon.button.initialModalButton.modal-accept-button")))
            next_button.click()

            terms_checkbox = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "label[for='modal-terms-checkbox']")))
            terms_checkbox.click()

            finish_button = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "button.modal-accept-button:not(.initialModalButton)")))
            finish_button.click()

            ok_button = wait.until(EC.element_to_be_clickable((By.XPATH, ".//button[normalize-space()='OK']")))
            ok_button.click()
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
            messages_button = wait.until(EC.element_to_be_clickable((By.ID, "navigation-messages-item")))
            messages_button.click()

            new_message_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="root"]/div/div/div[2]/div/div[1]/div/div[1]/div/div[1]/button')))
            new_message_btn.click()

            num_input = wait.until(EC.element_to_be_clickable((By.ID, "conversations-to-phone-number-input")))
            num_input.clear()
            num_input.send_keys(customer_number)

            message_input = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "message-input.text-message-input")))
            message_input.clear()
            message_input.send_keys(dispatch_msg)
            
            send_button = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "message-input-send")))
            send_button.click()
            
            time.sleep(5)
        except Exception:
            pass       

    finally:
        driver.quit()

def send_testing_message():
    send_message(os.environ.get('TALKROUTE_TEST_PHONE', ''), "testing")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    send_testing_message()
