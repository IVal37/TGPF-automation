# imports from std lib
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# imports from third party
from celery import Task, shared_task
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

# imports from project
from orders.constants import PaymentType
from orders.models import Order, OrderItem, OrderStatus
from orders.services import restock
from orders.services.customers import customer_label
from orders.services.note import get_order_notes
from orders.services.notices import add_notice
from orders.services.pricing import adjusted_total, effective_total, round_money
from orders.services.scrapers.errors import BeforeCommitError, describe, failed_step
from orders.services.scrapers.talkroute import send_message
from orders.services.scrapers.webjoint import (
    create_product,
    dispatch_to_driver,
    fill_order_notes,
    prepare_order,
    pull_from_chiles,
)
from orders.services.scrapers.weedmaps import get_wm_payment_type
from orders.services.telegram import send_telegram_message

logger = logging.getLogger(__name__)


# ── Browser tasks ───────────────────────────────────────────────────────────
# Each of these launches a real Chrome browser and can take anywhere from a few
# seconds to over a minute. Views enqueue them with .delay(...) instead of
# calling the scraper directly, so a webhook or a dispatcher click returns
# immediately.
#
# Failures are never silent and never double-send:
#  - A scraper raises BeforeCommitError when it fails BEFORE its irreversible
#    click (nothing went out), and OrderTask retries those automatically
#    (2 retries, backoff 30s then 60s).
#  - Any other error means it failed at or after that click and may have gone
#    through, so it is never retried automatically (and acks_late stays off).
#  - Once retries are used up, OrderTask saves the failure on the order
#    (Order.last_error...), where the dispatch card shows it in red with Retry
#    and Dismiss, and logs it. Tasks with no order raise a dashboard notice.


def failure_text(action: str, exc: BaseException, order=None, ref: str = "") -> str:
    step = failed_step(exc)
    # a BeforeCommitError's own message is already "ExcType: message" of its cause
    detail = str(exc) if isinstance(exc, BeforeCommitError) else describe(exc)
    who = (
        f" for {customer_label(order.customer_name)} (order {order.order_id})"
        if order
        else (f" (order {ref})" if ref else "")
    )
    text = f"{action} failed{who} at step '{step}': {detail}"
    if not isinstance(exc, BeforeCommitError):
        text += " — it may have gone through; check before retrying."
    return text


class OrderTask(Task):
    autoretry_for = (BeforeCommitError,)
    retry_backoff = 30
    max_retries = 2

    action = "Browser task"  # shown in the failure text
    # which Order field the task's first argument identifies, or None if it is
    # not about one order
    order_key = "order_id"

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        record_failure(self, exc, args, kwargs)


# called once a task has failed for good
def record_failure(task, exc, args, kwargs) -> None:
    ref = ""
    order = None
    if task.order_key and args:
        ref = str(args[0])
        order = Order.objects.filter(**{task.order_key: ref}).first()
    text = failure_text(task.action, exc, order, ref)
    logger.error("%s", text)

    if order is None:
        add_notice(text, "error")
        return
    Order.objects.filter(pk=order.pk).update(
        last_error=text,
        last_error_at=timezone.now(),
        last_error_task=task.name,
        last_error_args={"args": list(args), "kwargs": dict(kwargs)},
        last_error_after_commit=not isinstance(exc, BeforeCommitError),
    )


@shared_task(base=OrderTask, action="Dispatch text")
def send_message_task(order_id: str, phone: str, message: str) -> None:
    send_message(phone, message, order_ref=order_id)
    # the text is out: the order moves from sending to sent (only if it is still
    # sending, so a dispatcher's Dismiss or a later change isn't undone)
    Order.objects.filter(order_id=order_id, status=OrderStatus.SENDING).update(status=OrderStatus.SENT)


@shared_task(base=OrderTask, action="WebJoint note", order_key="webjoint_id")
def fill_order_notes_task(order_id: str, order_notes: str) -> None:
    fill_order_notes(order_id, order_notes)


@shared_task(base=OrderTask, action="Chiles pull", order_key="webjoint_id")
def pull_from_chiles_task(order_id: str) -> None:
    pull_from_chiles(order_id)


@shared_task(base=OrderTask, action="Driver dispatch", order_key="webjoint_id")
def dispatch_to_driver_task(order_id: str) -> None:
    dispatch_to_driver(order_id)


# The scraper to re-run on its own for each step of prepare_order that failed before its commit
_STEP_TASKS = {
    "dispatch_to_driver": dispatch_to_driver_task,
    "pull_from_chiles": pull_from_chiles_task,
}


# Dispatch the order to a driver (when `dispatch` is true, i.e. it is still Pending) and pull its
# items from Chiles, as one browser job with ONE WebJoint login, so the two steps never interleave
# with another order's. order_id is the WebJoint _id.
#
# A step that fails must not stop the other (the driver still needs the order even if the pull
# failed, and the pull is still worth doing if the dispatch did), and a retry must not repeat a
# step that already worked. prepare_order catches each step's failure and returns it. One that
# failed BEFORE its commit is handed to its own task, which retries by itself; one that failed
# after its commit is raised here (never retried). Either way no step is run twice. A failed login
# raises BeforeCommitError before any step starts, and the whole task is retried.
@shared_task(base=OrderTask, action="Order prep (pull/dispatch)", order_key="webjoint_id")
def prepare_order_task(order_id: str, dispatch: bool) -> None:
    after_commit = []
    for failure in prepare_order(order_id, dispatch):
        if isinstance(failure.error, BeforeCommitError):
            _STEP_TASKS[failure.step].delay(order_id)  # nothing happened yet, so it can retry on its own
        else:
            after_commit.append(failure.error)
    if after_commit:
        raise after_commit[0]


# looks up the Weedmaps payment type for a Weedmaps-sourced order and saves it.
# order_id is our Order.order_id. The save only fills an empty payment_type, so
# it never overwrites a value a dispatcher already set with "Send as cash".
# write_notes: for a customer we never text, new_order skips the immediate
# WebJoint note (it would be written before the payment type is known); write
# it here once the type is saved.
@shared_task(base=OrderTask, action="Payment type lookup")
def resolve_payment_type_task(order_id: str, write_notes: bool = False) -> None:
    order = Order.objects.filter(order_id=order_id).first()
    if order is None:
        logger.warning("resolve_payment_type (order %s): no such order", order_id)
        return

    payment_type = get_wm_payment_type(order.weedmaps_order_id)
    saved = Order.objects.filter(order_id=order_id, payment_type="").update(payment_type=payment_type)
    if saved:
        # the lookup finished: the order can now be approved and sent
        Order.objects.filter(order_id=order_id, status=OrderStatus.AWAITING_PAYMENT).update(
            status=OrderStatus.AWAITING_APPROVAL,
        )
    else:
        logger.info("resolve_payment_type (order %s): payment type already set; kept it", order_id)

    if write_notes:
        order.refresh_from_db()
        if order.webjoint_id:
            fill_order_notes(order.webjoint_id, get_order_notes(order))
        else:
            logger.warning("resolve_payment_type (order %s): no WebJoint id; note skipped", order_id)


@shared_task(base=OrderTask, action="Product creation", order_key=None)
def create_product_task(product_dicts: List[Dict[str, str]]) -> None:
    create_product(product_dicts)


# Until the real link is automated (the browser scraper only logs in), this just
# tells the dispatcher what to send by hand: it appears as a dashboard notice
# and in the log. Not a browser task, so it runs on the default queue.
@shared_task
def send_merchant_pay_link_task(order_id: str) -> None:
    order = Order.objects.filter(order_id=order_id).first()
    if order is None:
        logger.warning("send_merchant_pay_link (order %s): no such order", order_id)
        return
    amount = round_money(adjusted_total(effective_total(order), PaymentType.MERCHANT_PAY))
    message = f"Send merchant pay link to {customer_label(order.customer_name)} for ${amount}"
    logger.info("%s (order %s)", message, order_id)
    add_notice(f"{message} (order {order_id})")


# the tasks the dashboard's Retry button may re-queue, by task name
RETRYABLE_TASKS = {
    task.name: task
    for task in (
        send_message_task,
        fill_order_notes_task,
        pull_from_chiles_task,
        dispatch_to_driver_task,
        prepare_order_task,
        resolve_payment_type_task,
    )
}


# builds the "product x qty" text of the 3rd St digest: today's storefront sales. (The delivery
# digest is the dashboard's own Restock List, see restock.restock_message.) Queried straight from
# the DB rather than any in-memory list, since these tasks run in a separate Celery worker
# process from the Django web server.
def _build_restock_message(status: str) -> str:
    today = timezone.now().date()
    rows = (
        OrderItem.objects.filter(order__status=status, order__order_date__date=today)
        .values("product_name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("product_name")
    )
    if not rows:
        return "No items to restock today."
    return "\n".join(f"{row['product_name']} x{row['total_qty']}" for row in rows)


# How late a scheduled digest may still go out. Celery beat fires a job it missed while it was off
# the moment it starts, and a queued job waits for a worker that was down, so without this check
# restarting the stack at 7am would post last night's digest. A digest is a closing-time summary:
# late is fine, hours late is just noise.
DIGEST_GRACE = timedelta(minutes=30)


def _local_now() -> datetime:
    """The current time in the project's time zone. With USE_TZ off (as it is here) Django's now()
    is already local and naive, and localtime() refuses a naive value."""
    return timezone.localtime() if settings.USE_TZ else timezone.now()


def is_stale_digest(hour: int, minute: int = 0, now: Optional[datetime] = None) -> bool:
    """True when a digest scheduled for hour:minute (local time) runs more than DIGEST_GRACE after
    the latest such time, which means it is a catch-up and not its real run."""
    now = now or _local_now()
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due > now:
        due -= timedelta(days=1)
    return now - due > DIGEST_GRACE


# `hour`/`minute` are the schedule's own time (set in CELERY_BEAT_SCHEDULE); a digest called by
# hand, without them, always goes out.
def _skip_stale(name: str, hour: Optional[int], minute: int) -> bool:
    if hour is None or not is_stale_digest(hour, minute):
        return False
    logger.warning("skipped the %s: it was due at %02d:%02d and it is now %s", name, hour, minute, _local_now())
    return True


@shared_task
def send_third_st_restock(hour: Optional[int] = None, minute: int = 0) -> None:
    if _skip_stale("3rd St restock digest", hour, minute):
        return
    send_telegram_message(_build_restock_message(OrderStatus.STOREFRONT))


@shared_task
def send_delivery_restock(hour: Optional[int] = None, minute: int = 0) -> None:
    if _skip_stale("delivery restock digest", hour, minute):
        return
    send_telegram_message(restock.restock_message())
