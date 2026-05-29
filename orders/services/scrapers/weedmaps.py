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

def get_wm_payment_type():
    opts = Options()
    opts.add_argument("--window-size=1280,720")

    driver = webdriver.Chrome(options=opts)
    wait = WebDriverWait(driver, 15)

    payment_type = "Cash"

    try:
        driver.get("https://weedmaps.com/login?return_url=https://admin.weedmaps.com/orders")

        # intial login
        try:          
            email = wait.until(EC.element_to_be_clickable((By.ID, "user_username")))
            email.clear()
            email.send_keys(os.environ.get('WEEDMAPS_EMAIL', ''))

            password = wait.until(EC.element_to_be_clickable((By.ID, "user_password")))
            password.clear()
            password.send_keys(os.environ.get('WEEDMAPS_PASSWORD', ''))

            login_button = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "btn.btn-signup-primary")))
            login_button.click()
        except Exception:
            pass

        # move to orders
        try:          
            all_orders_button = wait.until(EC.element_to_be_clickable((By.ID ,"side-nav-orders-all-orders")))
            all_orders_button.click()
        except Exception:
            pass

        # click into most recent order
        try:          
            recent_order = wait.until(EC.element_to_be_clickable((By.XPATH , '//*[@id="__next"]/div/div[2]/div/div[3]/div[2]/div[1]/div[2]/div[1]/div[1]/span/a')))
            recent_order.click()
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