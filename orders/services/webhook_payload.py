"""Reading a WebJoint "new order" webhook into a typed object.

parse_new_order checks the fields the app depends on once, up front, and raises
InvalidPayload (shown as HTTP 400) instead of failing later with a KeyError.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from orders.constants import DIRECT_SOURCES
from orders.services.time import parse_webhook_time


class InvalidPayload(ValueError):
    """The webhook body is missing something we need, or has a value we cannot read."""


def is_weedmaps_source(source: str) -> bool:
    """Did this order come through Weedmaps rather than our own POS/website?"""
    return source not in DIRECT_SOURCES


@dataclass(frozen=True)
class NewOrderPayload:
    order_id: str
    customer_name: str
    order_time: datetime
    source: str
    discount: Decimal
    subtotal: Decimal
    total: Decimal
    delivery_fee: Decimal
    items: List[dict]
    # only delivery orders need these; a storefront sale may omit them
    webjoint_id: str            # WebJoint's own key for the order; "" if the webhook has none
    weedmaps_order_id: str      # the number Weedmaps shows as "#49018286"; "" for direct orders
    city: str
    phone: str                  # digits as WebJoint sends them, without the leading "+"
    payment_type: str           # the label WebJoint attached ("Cash" for every Weedmaps order)
    is_pending: bool            # WebJoint status "Pending": the order still has to be dispatched
    scheduled_start: Optional[datetime]
    scheduled_end: Optional[datetime]

    def order_fields(self) -> Dict[str, Any]:
        """The fields every kind of order (delivery or storefront) is saved with."""
        return dict(
            order_id=self.order_id,
            customer_name=self.customer_name,
            order_time=self.order_time,
            source=self.source,
            discount=self.discount,
            subtotal=self.subtotal,
            total=self.total,
            delivery_fee=self.delivery_fee,
            items=self.items,
        )

    def require_delivery_fields(self) -> None:
        """A delivery order cannot be routed without a city and a phone number to text."""
        if not self.city:
            raise InvalidPayload("missing shipping.city")
        if not self.phone:
            raise InvalidPayload("missing customer.phone")


def _get(data: Any, *path: str, default: Any = None) -> Any:
    """data[path[0]][path[1]]..., or default when any step is missing or null."""
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
        if data is None:
            return default
    return data


def _require(data: dict, *path: str) -> Any:
    value = _get(data, *path)
    if value in (None, ""):
        raise InvalidPayload(f"missing {'.'.join(path)}")
    return value


def _decimal(data: dict, key: str) -> Decimal:
    try:
        return Decimal(str(data.get(key, "0")))
    except InvalidOperation:
        raise InvalidPayload(f"{key} is not a number: {data.get(key)!r}") from None


def _time(value: Any, name: str) -> datetime:
    try:
        return parse_webhook_time(value)
    except (ValueError, AttributeError, TypeError):
        raise InvalidPayload(f"{name} is not a timestamp: {value!r}") from None


def _payment_label(data: dict) -> str:
    first_payment = (data.get("payments") or [{}])[0]
    return str((first_payment.get("paymentMethod") or {}).get("label", ""))


def parse_new_order(data: Any) -> NewOrderPayload:
    if not isinstance(data, dict):
        raise InvalidPayload("body is not a JSON object")

    cart = _get(data, "cart", default={})
    scheduled = (
        _get(cart, "deliveryTiming") == "Scheduled" and _get(cart, "scheduledStart") and _get(cart, "scheduledEnd")
    )
    phone = str(_get(data, "customer", "phone", default=""))

    return NewOrderPayload(
        order_id=str(_require(data, "id")),
        customer_name=str(_require(data, "customer", "name")),
        order_time=_time(_require(data, "created"), "created"),
        source=str(data.get("source") or ""),
        discount=_decimal(data, "discount"),
        subtotal=_decimal(data, "subtotal"),
        total=_decimal(data, "total"),
        delivery_fee=_decimal(data, "delivery"),
        items=data.get("details") or [],
        webjoint_id=str(data.get("_id") or ""),
        weedmaps_order_id=str(data.get("weedmapsOrderId") or ""),
        city=str(_get(data, "shipping", "city", default="")),
        phone=phone[1:],
        payment_type=_payment_label(data),
        is_pending=data.get("status") == "Pending",
        scheduled_start=_time(cart["scheduledStart"], "cart.scheduledStart") if scheduled else None,
        scheduled_end=_time(cart["scheduledEnd"], "cart.scheduledEnd") if scheduled else None,
    )
