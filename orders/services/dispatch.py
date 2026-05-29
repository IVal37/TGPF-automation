# imports from std lib
from random import choice
from decimal import Decimal
from datetime import datetime
from typing import List

# imports from project
from orders.services.money import round_money
from orders.services.eta import get_eta
from orders.constants import CITY_MINS, MERCHANT_PAY_CUSTOMERS, VALEDICTIONS
from orders.services.scrapers.weedmaps import get_wm_payment_type
from orders.services.state import driver_id_by_order_id, drivers_by_id

# gets relevant items from full JSON dict
# @params:
#   full_dict - JSON dict from order
# @returns:
#   ret_dict - dict with values to be used
def extract_msg_info(full_dict):
    ret_dict = {
        "id": str(full_dict["id"]),
        "time": datetime.now(),
        "city": full_dict["shipping"]["city"],

        "name": full_dict["customer"]["name"].split()[0].capitalize(),
        "last_name": full_dict["customer"]["name"].split()[1].capitalize(),

        "phone": full_dict["customer"]["phone"][1:],

        "pay_type": full_dict["payments"][0]["paymentMethod"]["label"],        
        "discount": round_money(Decimal(str(full_dict["discount"]))),
        "sub_total": Decimal(str(full_dict["subtotal"])),
        "total": round_money(Decimal(str(full_dict["total"]))),

        "source": full_dict["source"],
    }

    return ret_dict

# get message to send over TalkRoute
# @params:
#   dict - dict with needed values
# @returns:
#   ret_msg - message to be sent to customer
def get_dispatch_msg(dict):
    city = dict["city"]
    city_min = CITY_MINS.get(city, Decimal("0.00"))
        
    calculated_min_for_order = CalculateForMinCheck(dict)

    if calculated_min_for_order > city_min:
        ret_msg = normal_dispatch_msg(dict)
    else:
        ret_msg = under_min_msg(dict, city_min, calculated_min_for_order)
    
    return ret_msg

# calculate total used for minimum check
# @params:
#   dict - parsed dict with elements to be used
# @returns:
#   min_total - total before taxes and after discounts
def CalculateForMinCheck(dict):
    sub_total = dict["sub_total"]
    discount = dict["discount"]

    min_total = Decimal(str(sub_total)) - Decimal(str(discount))

    return min_total
    
# create normal dispatch message parsing dict info
# @params:
#   dict - parsed dict with elements to be used
# @returns:
#   dispatch_msg - message to send customer with all necessary info
def normal_dispatch_msg(dict):
    payment_type = (
        dict["pay_type"]
        if (dict["source"]) in ("POS", "Website")
        else get_wm_payment_type()
    )
    order_total = get_adjusted_total(dict["total"], payment_type)
    rounded_total = round_money(order_total)
    
    driver_id = driver_id_by_order_id[dict["id"]]
    driver = drivers_by_id[driver_id]

    in_area_bool = (dict["city"] == driver.get_current_city())
    if in_area_bool:
        in_area_text = "is in the area"
    else:
        early_eta, late_eta = get_eta(dict["id"])
        in_area_text = f"will be in the area between {early_eta}-{late_eta}"

    card_fee_text = "" if payment_type in ("Cash", "Merchant Pay - ACH") else "including card fee "

    merch_pay_used_bool = True if (dict["name"] + ' ' + dict["last_name"]) in MERCHANT_PAY_CUSTOMERS else False
    merch_pay_fee_text = "including Merchant Pay fee " if merch_pay_used_bool else ""
    merch_pay_used_text = "I will send over your merchant pay link in a moment. " if merch_pay_used_bool else ""

    rand_valediction = choice(VALEDICTIONS)

    dispatch_msg = (
        f'Hi {dict["name"]}, thank you for your order from TGPF. You saved ${dict["discount"]} with discounts ' +
        f'and your total {card_fee_text}{merch_pay_fee_text}is ${rounded_total}. {merch_pay_used_text}Your driver ' +
        f'{in_area_text} and you will get an auto update when he is directly en route to you and when he arrives. ' +
        f'{rand_valediction}'
    )

    return dispatch_msg

# create message for orders below minimum
# @params:
#   dict - parsed dict with elements to be used
#   city_min - minimum to deliver to customer's city
#   order_min_amount - calculated min for customer
# @returns:
#   under_min_msg - message to send customer with info about being under minimum
def under_min_msg(dict, city_min, order_min_amount):
    # difference between city minimum and calculated order minimum
    orderDiff = city_min - order_min_amount

    # just cancel order if big min and small order
    if city_min > Decimal("100.00") and orderDiff > (city_min / 2):
        options_text = "I am going to go ahead and cancel this order, please reach out or place a new order."
    else:
        options_text = "Could we add something to your order to get you above the minimum?"

    under_min_msg = (
        f'Hi {dict["name"]}, thank you for your order from TGPF. I am reaching out to let you know we ' +
        f'have an order minimum of ${city_min} to deliver out to {dict["city"]}. These minimums are ' +
        f'calculated before taxes and after discounts. You are currently at ${order_min_amount}. ' +
        f'{options_text} Thank you for your understanding!'
    )

    return under_min_msg

# create text to be placed in file for dispatcher records
# @params:
#   phone_num - customer phone number to send message to
#   msg_text - message to be sent to customer
#   items - list of items customer purchased
# @returns:
#   msg_str - full text to write in file
def get_dispatch_info(phone_num: str, msg_text: str, order_notes_text: str, items: List[str], item_num: int): 
    msg_str = f"{phone_num}\n\n{msg_text}\n\n{order_notes_text}\n\nItems Purchased ({item_num}): \n"
    for item in items:
        append_str = '\t' + item + '\n' 
        msg_str += append_str
    
    return msg_str

# adjust total based on chosen payment type
# send external payment link if needed
# @params:
#   base_total - unadjusted total for order
#   payment_type - chosen payment type for order
# @returns:
#   ret_total - adjusted total to be payed
def get_adjusted_total(base_total, payment_type):
    if payment_type == "Cash":
        ret_total = base_total
    elif payment_type == "Merchant Pay - ACH":
        #send_merch_pay(base_total)
        ret_total = base_total + (base_total * Decimal("0.0225")) + Decimal("0.35")
    else:
        ret_total = base_total * Decimal("1.03")
    
    return Decimal(ret_total)


# builds list of purchased items from dict
# @params:
#   item_dict - parsed dict with section containing item info
# @returns:
#   item_list - list with all purchased items and their details
def populate_items(item_dict):
    item_list = []
    item_count = 0

    for item in item_dict:
        item_name = item["variant"]["product"]["name"]
        item_brand = item["variant"]["product"]["brand"]["name"]
        item_num = item["quantity"]
        
        item_info = f'{item_name} | {item_brand} ({item_num})'
        item_list.append(item_info)
        item_count += item_num

    return item_list, item_count
