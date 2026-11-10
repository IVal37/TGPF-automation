# imports from std lib
import logging
from typing import Dict, List

# imports from third party
from celery import shared_task
from django.db.models import Sum
from django.utils import timezone

# imports from project
from orders.models import Order, OrderItem, OrderStatus
from orders.services.note import get_order_notes
from orders.services.telegram import send_telegram_message
from orders.services.scrapers.talkroute import send_message
from orders.services.scrapers.webjoint import create_product, dispatch_to_driver, fill_order_notes, pull_from_chiles
from orders.services.scrapers.weedmaps import get_wm_payment_type
from orders.services.scrapers.merchant_pay import send_merchant_pay_link

logger = logging.getLogger(__name__)


# ── Selenium call sites, moved off the request thread ───────────────────────
# Each of these launches a real Chrome browser and can take anywhere from a
# few seconds to over a minute. Views enqueue these with .delay(...) instead
# of calling the scraper directly, so a webhook or a dispatcher click returns
# immediately instead of blocking the whole app on a browser session. The
# underlying scraper functions already catch and log their own exceptions
# (never raise), so there's nothing for Celery's retry machinery to act on —
# these are plain fire-and-forget wrappers, not retrying ones.

@shared_task
def send_message_task(phone: str, message: str) -> None:
    send_message(phone, message)

@shared_task
def fill_order_notes_task(order_id: str, order_notes: str) -> None:
    fill_order_notes(order_id, order_notes)

@shared_task
def pull_from_chiles_task(order_id: str) -> None:
    pull_from_chiles(order_id)

@shared_task
def dispatch_to_driver_task(order_id: str) -> None:
    dispatch_to_driver(order_id)

# pull the order's items from Chiles, then (for orders still Pending) dispatch
# it to a driver, as one browser job so the two steps never interleave with
# another order's. order_id is the WebJoint _id. A pull that raises must not
# stop the dispatch: the driver still needs the order.
@shared_task
def prepare_order_task(order_id: str, dispatch: bool) -> None:
    try:
        pull_from_chiles(order_id)
    except Exception:
        logger.exception("prepare_order (order %s): pull_from_chiles failed; continuing", order_id)
    if dispatch:
        dispatch_to_driver(order_id)

# looks up the Weedmaps payment type for a Weedmaps-sourced order and saves it.
# order_id is our Order.order_id. The save only fills an empty payment_type, so
# it never overwrites a value a dispatcher already set with "Send as cash".
# write_notes: for a customer we never text, new_order skips the immediate
# WebJoint note (it would be written before the payment type is known); write
# it here once the type is saved.
@shared_task
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

@shared_task
def create_product_task(product_dicts: List[Dict[str, str]]) -> None:
    create_product(product_dicts)

@shared_task
def send_merchant_pay_link_task() -> None:
    send_merchant_pay_link()


# builds the "product x qty" restock text for today's orders of one status:
# storefront sales for the 3rd St digest, completed deliveries for the delivery
# digest (cancelled and still-open orders are not sales). Queried straight from
# the DB rather than any in-memory list, since these tasks run in a separate
# Celery worker process from the Django web server.
def _build_restock_message(status: str) -> str:
    today = timezone.now().date()
    rows = (
        OrderItem.objects
        .filter(order__status=status, order__order_date__date=today)
        .values('product_name')
        .annotate(total_qty=Sum('quantity'))
        .order_by('product_name')
    )
    if not rows:
        return "No items to restock today."
    return "\n".join(f"{row['product_name']} x{row['total_qty']}" for row in rows)


@shared_task
def send_third_st_restock():
    send_telegram_message(_build_restock_message(OrderStatus.STOREFRONT))


@shared_task
def send_delivery_restock():
    send_telegram_message(_build_restock_message(OrderStatus.COMPLETED))
