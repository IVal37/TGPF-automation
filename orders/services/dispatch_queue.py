"""Texting customers and writing WebJoint notes for orders, and the dispatcher's actions on the Dispatch Queue.

Every SMS and every WebJoint write goes through queue_text / queue_webjoint here, so the
"texts nobody twice" rule lives in one place. Browser work is only ever enqueued as a task,
never run inline.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from django.db.models import Q

from orders import tasks
from orders.constants import PaymentType
from orders.models import OPEN_STATUSES, QUEUE_STATUSES, Driver, Order, OrderStatus
from orders.services import config, queries
from orders.services.customer_texts import dispatch_text, reschedule_text
from orders.services.customers import is_fsa_fsm
from orders.services.engine import assign_order as assign_order_to_driver
from orders.services.note import get_order_notes

logger = logging.getLogger(__name__)


class ActionRejected(Exception):
    """The dispatcher's action was not carried out; the message says why and is shown to them."""


class NotFound(Exception):
    """The order (or driver) the action names does not exist, or is not in a state it applies to."""


def mark_sent(order_id: str) -> None:
    """The customer is dealt with: move the order out of the queue to "sent"."""
    Order.objects.filter(order_id=order_id).update(status=OrderStatus.SENT)


def queue_text(order_id: str, phone: str, text: str) -> None:
    """Text the customer through the browser queue. The order goes to "sending" FIRST (no
    longer in the queue, so a second Send click is a 404 and can't text twice);
    send_message_task moves it to "sent" once the text is out, or leaves it on "sending"
    with the error shown on its card if it fails."""
    Order.objects.filter(order_id=order_id).update(status=OrderStatus.SENDING)
    tasks.send_message_task.delay(order_id, phone, text)


def queue_webjoint(task, order_id: str, webjoint_id: str, *args) -> None:
    """Queue a WebJoint browser task for exactly one order, identified by its WebJoint id.
    Orders saved without one (older orders, or a payload without _id) are skipped with a
    warning: acting on "some" order is exactly the bug the per-order id avoids."""
    if not webjoint_id:
        logger.warning("no WebJoint id for order %s; skipped %s", order_id, task.name)
        return
    task.delay(webjoint_id, *args)


def clear_queue() -> None:
    """Dismiss every card on the Dispatch Queue without texting anyone or writing any WebJoint
    note: a cleanup/testing utility, not a bulk send. Real pending orders cleared this way
    never get their dispatch text."""
    queries.pending_orders().update(status=OrderStatus.SENT)


def clear_error(order: Order) -> None:
    Order.objects.filter(pk=order.pk).update(
        last_error="",
        last_error_at=None,
        last_error_task="",
        last_error_args={},
        last_error_after_commit=False,
    )


def _get_queued(order_id: Optional[str]) -> Order:
    order = Order.objects.filter(order_id=order_id, status__in=QUEUE_STATUSES).first()
    if order is None:
        raise NotFound(f"order {order_id} is not in the queue")
    return order


def send_dispatch(order_id: str, eta_start: str = "", eta_end: str = "", as_cash: bool = False) -> None:
    """Send the dispatch text and WebJoint note with the (possibly edited) ETA window.
    eta_start/eta_end are "HH:MM" strings from the form."""
    order = _get_queued(order_id)

    if order.driver_id is None:
        raise ActionRejected(f"Order {order_id} has no driver yet, so there is no ETA window to send.")

    if not order.payment_type:
        # the payment type changes the price in the text; sending before it's known would quote the wrong total
        if not as_cash:
            raise ActionRejected(f"Order {order_id}: the payment type is not known yet, so it was not sent.")
        order.payment_type = PaymentType.CASH
        order.save()
    elif as_cash:
        # the lookup finished after this page loaded; show the real value first
        raise ActionRejected(
            f'Order {order_id} now has payment type {order.payment_type}; "Send as cash" was ignored. '
            "Check the card and send again."
        )

    if eta_start and eta_end:
        try:
            start_time = datetime.strptime(eta_start, "%H:%M").time()
            end_time = datetime.strptime(eta_end, "%H:%M").time()
        except ValueError:
            raise ActionRejected(f'Order {order_id}: "{eta_start}" - "{eta_end}" is not a valid time window.') from None
        base_date = order.eta_start.date() if order.eta_start else datetime.now().date()
        order.set_etas(datetime.combine(base_date, start_time), datetime.combine(base_date, end_time))

    queue_text(order_id, order.customer_phone, dispatch_text(order))
    queue_webjoint(tasks.fill_order_notes_task, order.order_id, order.webjoint_id, get_order_notes(order))


def send_reschedule(order_id: str) -> None:
    order = _get_queued(order_id)
    queue_text(order_id, order.customer_phone, reschedule_text(order))
    queue_webjoint(
        tasks.fill_order_notes_task,
        order.order_id,
        order.webjoint_id,
        "reached out about rescheduling for tomorrow morning",
    )


def assign_driver(order_id: str, driver_id: Optional[str]) -> None:
    """Hand an unassigned order to a driver. A queued order gets its ETA now and becomes a
    normal Dispatch Queue card; one that was already texted just gets the driver (the customer
    keeps the ETA they were told)."""
    order = Order.objects.filter(
        order_id=order_id,
        status__in=OPEN_STATUSES,
        is_shell=False,
        driver__isnull=True,
    ).first()
    driver = Driver.objects.filter(pk=driver_id, is_active=True).first() if str(driver_id).isdigit() else None
    if order is None or driver is None:
        raise NotFound(f"order {order_id} or driver {driver_id} cannot be assigned")
    was_queued = order.status in QUEUE_STATUSES

    assign_order_to_driver(order.order_id, driver.pk)

    if was_queued and config.is_no_text_customer(order.customer_name):
        # new orders skip this customer's notes while there is no driver/ETA
        fresh = Order.objects.get(order_id=order.order_id)
        if fresh.payment_type:
            queue_webjoint(tasks.fill_order_notes_task, fresh.order_id, fresh.webjoint_id, get_order_notes(fresh))
        else:
            # the payment lookup is still running; its task writes the note once it lands
            tasks.resolve_payment_type_task.delay(fresh.order_id, write_notes=True)
        mark_sent(fresh.order_id)


def link_orders(order_id: str, primary_order_id: str) -> None:
    """Mark an FSA/FSM order as the second part of a legal-limit split: it is fully absorbed
    into the primary order (no SMS, no queue entry of its own), and only a note on its own
    WebJoint order says so. The primary's SMS and notes then reflect the combined total
    (see pricing.effective_total)."""
    companion = _get_queued(order_id)
    primary = _get_queued(primary_order_id)
    if not is_fsa_fsm(companion.customer_name):
        raise ActionRejected(f"Order {order_id} is not a Frank Sinatra order, so it cannot be linked.")

    companion.linked_order = primary
    companion.status = OrderStatus.SENT
    companion.save()

    queue_webjoint(
        tasks.fill_order_notes_task,
        companion.order_id,
        companion.webjoint_id,
        f"second part of {primary.customer_name}'s order",
    )


def unlink_order(order_id: str) -> None:
    """Reverse link_orders: put the FSA/FSM order back in the queue for normal handling."""
    companion = Order.objects.filter(order_id=order_id, status__in=OPEN_STATUSES).first()
    if companion is None:
        raise NotFound(f"order {order_id} is not open")
    companion.linked_order = None
    # back in the queue; Send stays held if its payment type was never filled in
    companion.status = OrderStatus.AWAITING_APPROVAL if companion.payment_type else OrderStatus.AWAITING_PAYMENT
    companion.save()


def retry_failed_task(order_id: str) -> None:
    """Re-queue the browser task that failed for an order, with the same arguments, and clear the error."""
    order = Order.objects.filter(order_id=order_id, last_error__gt="").first()
    task = tasks.RETRYABLE_TASKS.get(order.last_error_task) if order else None
    if task is None:
        raise NotFound(f"no retryable task recorded for order {order_id}")
    payload = order.last_error_args or {}
    clear_error(order)
    task.delay(*payload.get("args", []), **payload.get("kwargs", {}))


def dismiss_error(order_id: str) -> None:
    """Clear an order's failure. For an order stuck on "sending" (with or without an error: a
    worker that died mid-send leaves none) the dispatcher is saying they handled the text
    themselves, so it becomes "sent"."""
    order = Order.objects.filter(Q(last_error__gt="") | Q(status=OrderStatus.SENDING), order_id=order_id).first()
    if order is None:
        raise NotFound(f"order {order_id} has no error to dismiss")
    clear_error(order)
    if order.status == OrderStatus.SENDING:
        mark_sent(order.order_id)
