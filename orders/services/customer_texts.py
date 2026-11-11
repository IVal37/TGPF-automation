"""The texts we send customers."""

from __future__ import annotations

from random import choice

from orders.constants import BIG_MINIMUM, VALEDICTIONS, PaymentType
from orders.models import Order
from orders.services import config
from orders.services.customers import split_name
from orders.services.hours import closing_dt
from orders.services.pricing import adjusted_total, effective_total, min_check_total, round_money
from orders.services.time import format_time


def render_dispatch_text(
    name: str,
    last_name: str,
    discount,
    total,
    payment_type: str,
    early_eta: str,
    late_eta: str,
) -> str:
    """The dispatch SMS from its parts. early_eta/late_eta are already-formatted times ("2:30")."""
    rounded_total = round_money(adjusted_total(total, payment_type))
    in_area_text = f"will be in the area between {early_eta}-{late_eta}"
    card_fee_text = "" if payment_type in (PaymentType.CASH, PaymentType.MERCHANT_PAY) else "including card fee "

    is_merchant_pay = config.is_merchant_pay_customer(name, last_name)
    merchant_pay_fee_text = "including Merchant Pay fee " if is_merchant_pay else ""
    merchant_pay_link_text = "I will send over your merchant pay link in a moment. " if is_merchant_pay else ""

    return (
        f"Hi {name}, thank you for your order from TGPF. You saved ${discount} with discounts "
        + f"and your total {card_fee_text}{merchant_pay_fee_text}is ${rounded_total}. {merchant_pay_link_text}Your driver "
        + f"{in_area_text} and you will get an auto update when he is directly en route to you and when he arrives. "
        + f"{choice(VALEDICTIONS)}"
    )


def dispatch_text(order: Order) -> str:
    """The dispatch SMS for an order at send time, using the ETA window on the order right
    now (what the dispatcher approved or edited). Pass an order freshly read from the DB."""
    name, last_name = split_name(order.customer_name)
    return render_dispatch_text(
        name,
        last_name,
        order.discount,
        effective_total(order),
        order.payment_type,
        format_time(order.eta_start),
        format_time(order.eta_end),
    )


def reschedule_text(order: Order) -> str:
    """The "driver won't make it out tonight" text, for an order whose ETA is past closing."""
    name, _ = split_name(order.customer_name)
    close_time = closing_dt(order.eta_start)
    close_str = f"{close_time.hour % 12 or 12}pm"
    return (
        f"Hi {name}, thank you for your order from TGPF. "
        f"Unfortunately our driver will not be able to make it out to you by "
        f"{close_str} tonight. I can cancel your order or reschedule it for first "
        f"thing in the morning, let me know what works. Thanks!"
    )


def under_min_text(first_name: str, city: str, subtotal, discount) -> str:
    """The text for an order below its city's minimum."""
    city_min = config.city_min(city)
    order_total = min_check_total(subtotal, discount)
    shortfall = city_min - order_total

    # a big minimum and a small order: just cancel
    if city_min > BIG_MINIMUM and shortfall > (city_min / 2):
        options_text = "I am going to go ahead and cancel this order, please reach out or place a new order."
    else:
        options_text = "Could we add something to your order to get you above the minimum?"

    return (
        f"Hi {first_name}, thank you for your order from TGPF. I am reaching out to let you know we "
        + f"have an order minimum of ${city_min} to deliver out to {city}. These minimums are "
        + f"calculated before taxes and after discounts. You are currently at ${order_total}. "
        + f"{options_text} Thank you for your understanding!"
    )
