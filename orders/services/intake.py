"""Handling a new WebJoint order: save it, route it, and queue the texts and WebJoint work it needs.

The webhook view only parses the request and calls handle_new_order. Every SMS and WebJoint
write here is gated by RESTOCK_TEST_MODE.
"""
from __future__ import annotations

import logging

from django.conf import settings

from orders import tasks
from orders.models import Order, OrderStatus, SpecialAddress
from orders.services import config
from orders.services.customer_texts import under_min_text
from orders.services.customers import split_name
from orders.services.dispatch_queue import mark_sent, queue_text, queue_webjoint
from orders.services.engine import add_order, add_storefront_order
from orders.services.eta import estimate_window
from orders.services.note import get_order_notes
from orders.services.pricing import is_under_min, round_money
from orders.services.webhook_payload import NewOrderPayload, is_weedmaps_source, parse_new_order

logger = logging.getLogger(__name__)

UNDER_MIN_NOTE = "reached out about under min"


def handle_new_order(data: dict) -> None:
    """Process one "new order" webhook body. Raises InvalidPayload if it is unusable."""
    # special addresses live in the database (SpecialAddress, edited in admin): a "blocked"
    # one means don't create an order at all; the "storefront" (3rd St) is a real sale that
    # still needs tracking for the storefront restock digest (it always wins over blocked)
    special_address = config.match_special_address(data.get("shipping") or {})
    if special_address == SpecialAddress.Kind.BLOCKED:
        return

    payload = parse_new_order(data)

    # WebJoint resends a webhook it thinks failed (e.g. after a timeout); the order is
    # already saved and handled, so do nothing more
    if Order.objects.filter(order_id=payload.order_id).exists():
        logger.info("duplicate webhook for order %s; ignored", payload.order_id)
        return

    if special_address == SpecialAddress.Kind.STOREFRONT:
        # walk-in storefront pickup: no driver, no ETA, no dispatch SMS; just persisted
        # so the nightly 3rd St restock digest can pick it up
        add_storefront_order(**payload.order_fields())
        return

    payload.require_delivery_fields()
    _handle_delivery_order(payload)


def _handle_delivery_order(payload: NewOrderPayload) -> None:
    order_id = payload.order_id
    if not payload.webjoint_id:
        logger.warning("new_order %s: webhook has no _id; WebJoint actions will be skipped", order_id)

    # WebJoint's webhook always labels Weedmaps orders "Cash", so their real payment type is
    # looked up on Weedmaps by a background task; until it lands the order's payment_type stays
    # empty and the dashboard holds Send. In test mode nothing is scraped and the label is kept.
    resolve_payment = is_weedmaps_source(payload.source) and not settings.RESTOCK_TEST_MODE

    order = add_order(
        order_city=payload.city,
        customer_phone=payload.phone,
        webjoint_id=payload.webjoint_id,
        weedmaps_order_id=payload.weedmaps_order_id if resolve_payment else "",
        status=OrderStatus.AWAITING_PAYMENT if resolve_payment else OrderStatus.AWAITING_APPROVAL,
        # a scheduled order keeps the window the customer picked, so it can still be
        # applied if the order is assigned to a driver later
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        **payload.order_fields(),
    )
    # nobody was on shift: the order is saved without a driver and waits in the Dispatch
    # Queue as an Unassigned card (see dispatch_queue.assign_driver)
    unassigned = order.driver_id is None

    first_name, last_name = split_name(payload.customer_name)
    no_text = config.is_no_text_customer(payload.customer_name)
    under_min = is_under_min(payload.subtotal, round_money(payload.discount), payload.city)

    # an under-minimum order gets its text now (it has no ETA window to review). A normal
    # order gets its ETA window worked out and saved here; the text is built later, at send
    # time, once a dispatcher has approved or edited the window. An unassigned order has no
    # route to estimate from, so it waits for assign_driver.
    under_min_message = None
    if under_min:
        under_min_message = under_min_text(first_name, payload.city, payload.subtotal, round_money(payload.discount))
    elif not unassigned:
        estimate_window(order_id)
    Order.objects.filter(order_id=order_id).update(payment_type="" if resolve_payment else payload.payment_type)

    if settings.RESTOCK_TEST_MODE:
        return

    if under_min:
        # no ETA window in this message, nothing to double-check: auto-send
        if no_text:
            mark_sent(order_id)
        else:
            queue_text(order_id, payload.phone, under_min_message)
        queue_webjoint(tasks.fill_order_notes_task, order_id, payload.webjoint_id, UNDER_MIN_NOTE)
    elif no_text and not unassigned:
        # an excluded customer: we never text them, so there's nothing to hold for approval.
        # Write notes with the system-computed window now. For a Weedmaps order the note depends
        # on the payment type, which isn't known yet, so resolve_payment_type_task writes it.
        if not resolve_payment:
            fresh = Order.objects.get(order_id=order_id)
            queue_webjoint(tasks.fill_order_notes_task, order_id, payload.webjoint_id, get_order_notes(fresh))
        mark_sent(order_id)
    # else: a normal order with a real ETA window stays awaiting approval, in the Dispatch Queue
    # tab for a human to approve/edit and send (see dispatch_queue.send_dispatch)

    if config.is_merchant_pay_customer(first_name, last_name) and not under_min:
        tasks.send_merchant_pay_link_task.delay(order_id)

    # first in the browser queue, since a dispatcher is waiting on it
    if resolve_payment:
        tasks.resolve_payment_type_task.delay(order_id, write_notes=no_text and not under_min and not unassigned)
    # pull the order's items, and dispatch it if it's still Pending
    queue_webjoint(tasks.prepare_order_task, order_id, payload.webjoint_id, payload.is_pending)
