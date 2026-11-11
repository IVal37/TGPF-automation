# imports from std lib
from random import choice
from decimal import Decimal
from datetime import datetime

# imports from project
from orders.models import Order
from orders.services import config
from orders.services.money import round_money
from orders.services.eta import apply_scheduled_window, get_eta
from orders.services.time import format_time, parse_webhook_time
from orders.services.hours import closing_dt
from orders.constants import (
    BIG_MINIMUM, CARD_FEE_MULTIPLIER, DIRECT_SOURCES, FSA_FSM_NAMES, MERCHANT_PAY_FLAT_FEE,
    MERCHANT_PAY_RATE, VALEDICTIONS, PaymentType,
)

# is this the "Frank Sinatra Adult"/"Frank Sinatra Medical" placeholder
# account? (either a legal-limit split's second order, or an unrelated lazy
# 3rd St walk-in sale — see orders/views.py:link_order_view)
def is_fsa_fsm(customer_name: str) -> bool:
    return customer_name.strip().lower() in FSA_FSM_NAMES

def is_merchant_pay_customer(name: str, last_name: str) -> bool:
    return config.is_merchant_pay_customer(name, last_name)

# did this order come through Weedmaps rather than our own POS/website?
def is_weedmaps_source(source: str) -> bool:
    return source not in DIRECT_SOURCES

# splits a full customer name into capitalized (first, last); last is "" for
# single-word names
def split_name(full_name: str):
    parts = full_name.split()
    name = parts[0].capitalize() if parts else ""
    last_name = parts[1].capitalize() if len(parts) > 1 else ""
    return name, last_name

# gets relevant items from full JSON dict
# @params:
#   full_dict - JSON dict from order
# @returns:
#   ret_dict - dict with values to be used
def extract_msg_info(full_dict):
    name, last_name = split_name(full_dict["customer"]["name"])
    ret_dict = {
        "id": str(full_dict["id"]),
        "time": datetime.now(),
        "city": full_dict["shipping"]["city"],

        "name": name,
        "last_name": last_name,

        "phone": full_dict["customer"]["phone"][1:],

        "pay_type": ((full_dict.get("payments") or [{}])[0].get("paymentMethod") or {}).get("label", ""),
        "discount": round_money(Decimal(str(full_dict["discount"]))),
        "sub_total": Decimal(str(full_dict["subtotal"])),
        "total": round_money(Decimal(str(full_dict["total"]))),

        "source": full_dict["source"],
    }

    cart = full_dict.get("cart", {})
    delivery_timing = cart.get("deliveryTiming", "")
    ret_dict["delivery_timing"] = delivery_timing
    ret_dict["scheduled_start"] = cart.get("scheduledStart") if delivery_timing == "Scheduled" else None
    ret_dict["scheduled_end"] = cart.get("scheduledEnd") if delivery_timing == "Scheduled" else None

    return ret_dict

# get message to send over TalkRoute
# @params:
#   dict - dict with needed values
# @returns:
#   ret_msg - message to be sent to customer
def get_dispatch_msg(dict):
    if is_under_min(dict):
        return under_min_msg(dict, _city_min(dict), min_check_total(dict))
    return normal_dispatch_msg(dict)

def _city_min(dict):
    return config.city_min(dict["city"])

def is_under_min(dict):
    return min_check_total(dict) <= _city_min(dict)

# calculate total used for minimum check
# @params:
#   dict - parsed dict with elements to be used
# @returns:
#   min_total - total before taxes and after discounts
def min_check_total(dict):
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
    payment_type = dict["pay_type"]
    early_eta, late_eta = get_eta(dict["id"])

    if dict.get("delivery_timing") == "Scheduled" and dict.get("scheduled_end") and dict.get("scheduled_start"):
        sched_end_dt = parse_webhook_time(dict["scheduled_end"])
        order = Order.objects.get(order_id=dict["id"])
        sched_start_dt = parse_webhook_time(dict["scheduled_start"])
        if apply_scheduled_window(order, sched_start_dt, sched_end_dt):
            early_eta = format_time(sched_start_dt)
            late_eta = format_time(sched_end_dt)

    return render_dispatch_msg(dict["name"], dict["last_name"], dict["discount"], dict["total"], payment_type, early_eta, late_eta)

# renders the customer-facing dispatch SMS from its component parts.
# shared by normal_dispatch_msg (order-creation time, ETA fresh off get_eta)
# and build_dispatch_msg_from_order (send time, after a dispatcher approves or
# edits the ETA window) so both paths produce identical message formatting.
# @params:
#   name, last_name - customer name, used for the greeting and the
#     merchant-pay-customer lookup
#   discount, total, payment_type - order fields that affect pricing text
#   early_eta, late_eta - already-formatted ETA window strings (e.g. "2:30")
# @returns:
#   dispatch_msg - message to send customer with all necessary info
def render_dispatch_msg(name, last_name, discount, total, payment_type, early_eta, late_eta):
    order_total = get_adjusted_total(total, payment_type)
    rounded_total = round_money(order_total)

    in_area_text = f"will be in the area between {early_eta}-{late_eta}"

    card_fee_text = "" if payment_type in (PaymentType.CASH, PaymentType.MERCHANT_PAY) else "including card fee "

    merch_pay_used_bool = is_merchant_pay_customer(name, last_name)
    merch_pay_fee_text = "including Merchant Pay fee " if merch_pay_used_bool else ""
    merch_pay_used_text = "I will send over your merchant pay link in a moment. " if merch_pay_used_bool else ""

    rand_valediction = choice(VALEDICTIONS)

    dispatch_msg = (
        f'Hi {name}, thank you for your order from TGPF. You saved ${discount} with discounts ' +
        f'and your total {card_fee_text}{merch_pay_fee_text}is ${rounded_total}. {merch_pay_used_text}Your driver ' +
        f'{in_area_text} and you will get an auto update when he is directly en route to you and when he arrives. ' +
        f'{rand_valediction}'
    )

    return dispatch_msg

# returns the total actually owed for an order, accounting for a legal-limit
# split: if another order is linked to this one (a companion FSA/FSM order —
# see orders/views.py:link_order_view), the two are really one delivery, so
# their totals combine minus whichever of the two delivery fees is lower
# (never double-charge two delivery fees for one stop). Orders with no linked
# companion are unaffected — this just returns order.total unchanged.
def get_effective_total(order) -> Decimal:
    companion = Order.objects.filter(linked_order=order).first()
    if companion is None:
        return order.total
    lower_fee = min(order.delivery_fee, companion.delivery_fee)
    return order.total + companion.total - lower_fee

# rebuilds the dispatch SMS from an Order row at send time, using whatever
# eta_start/eta_end are currently on the order — i.e. the dispatcher-approved
# or dispatcher-edited window, not necessarily the original system suggestion.
# @params:
#   order - Order instance (should be freshly fetched from the DB so
#     payment_type/eta reflect the latest saved state)
# @returns:
#   dispatch_msg - message to send customer with the approved ETA window
def build_dispatch_msg_from_order(order) -> str:
    name, last_name = split_name(order.customer_name)
    return render_dispatch_msg(
        name, last_name, order.discount, get_effective_total(order), order.payment_type,
        format_time(order.eta_start), format_time(order.eta_end),
    )

# builds the "driver won't make it out tonight" reschedule text from an Order
# row, used when a dispatcher decides a past-closing order should be
# rescheduled rather than dispatched with a late-hours ETA.
# @params:
#   order - Order instance with eta_start set (used to determine the
#     business's closing hour for that day)
# @returns:
#   reschedule_msg - message offering to cancel or reschedule for the morning
def build_reschedule_msg_from_order(order) -> str:
    name, _ = split_name(order.customer_name)
    close_time = closing_dt(order.eta_start)
    close_str = f"{close_time.hour % 12 or 12}pm"

    return (
        f'Hi {name}, thank you for your order from TGPF. '
        f'Unfortunately our driver will not be able to make it out to you by '
        f'{close_str} tonight. I can cancel your order or reschedule it for first '
        f'thing in the morning, let me know what works. Thanks!'
    )

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
    if city_min > BIG_MINIMUM and orderDiff > (city_min / 2):
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

# adjust total based on chosen payment type
# send external payment link if needed
# @params:
#   base_total - unadjusted total for order
#   payment_type - chosen payment type for order
# @returns:
#   ret_total - adjusted total to be payed
def get_adjusted_total(base_total, payment_type):
    if payment_type == PaymentType.CASH:
        ret_total = base_total
    elif payment_type == PaymentType.MERCHANT_PAY:
        ret_total = base_total + (base_total * MERCHANT_PAY_RATE) + MERCHANT_PAY_FLAT_FEE
    else:
        ret_total = base_total * CARD_FEE_MULTIPLIER
    
    return Decimal(ret_total)

