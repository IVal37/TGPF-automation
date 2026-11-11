"""Money rules: rounding, payment fees, the order minimum and combined totals."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from orders.constants import CARD_FEE_MULTIPLIER, MERCHANT_PAY_FLAT_FEE, MERCHANT_PAY_RATE, PaymentType
from orders.models import Order
from orders.services import config


def round_money(amount: Decimal) -> Decimal:
    """Round to the nearest cent."""
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def adjusted_total(base_total: Decimal, payment_type: str) -> Decimal:
    """What the customer pays once the fee for their payment type is added."""
    if payment_type == PaymentType.CASH:
        total = base_total
    elif payment_type == PaymentType.MERCHANT_PAY:
        total = base_total + (base_total * MERCHANT_PAY_RATE) + MERCHANT_PAY_FLAT_FEE
    else:
        total = base_total * CARD_FEE_MULTIPLIER
    return Decimal(total)


def effective_total(order: Order) -> Decimal:
    """The total actually owed for an order, accounting for a legal-limit split.

    If another order is linked to this one (a companion FSA/FSM order, see
    dispatch_queue.link_orders) the two are really one delivery: their totals combine, minus
    whichever of the two delivery fees is lower (never charge two delivery fees for one stop).
    An order with no companion is unchanged."""
    companion = Order.objects.filter(linked_order=order).first()
    if companion is None:
        return order.total
    lower_fee = min(order.delivery_fee, companion.delivery_fee)
    return order.total + companion.total - lower_fee


def min_check_total(subtotal, discount) -> Decimal:
    """The amount checked against a city's minimum: before taxes, after discounts."""
    return Decimal(str(subtotal)) - Decimal(str(discount))


def is_under_min(subtotal, discount, city: str) -> bool:
    return min_check_total(subtotal, discount) <= config.city_min(city)
