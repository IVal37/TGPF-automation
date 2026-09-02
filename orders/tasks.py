# imports from std lib
from typing import Dict, List

# imports from third party
from celery import shared_task
from django.db.models import Sum
from django.utils import timezone

# imports from project
from orders.models import OrderItem
from orders.services.telegram import send_telegram_message
from orders.services.scrapers.talkroute import send_message
from orders.services.scrapers.webjoint import create_product, dispatch_to_driver, fill_order_notes, pull_from_chiles
from orders.services.scrapers.merchant_pay import send_merchant_pay_link


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
def fill_order_notes_task(order_notes: str) -> None:
    fill_order_notes(order_notes)

@shared_task
def pull_from_chiles_task() -> None:
    pull_from_chiles()

@shared_task
def dispatch_to_driver_task() -> None:
    dispatch_to_driver()

@shared_task
def create_product_task(product_dicts: List[Dict[str, str]]) -> None:
    create_product(product_dicts)

@shared_task
def send_merchant_pay_link_task() -> None:
    send_merchant_pay_link()


# builds the "product x qty" restock text for today's orders on one side of
# the business (storefront vs delivery). Queried straight from the DB rather
# than any in-memory list, since these tasks run in a separate Celery worker
# process from the Django web server.
def _build_restock_message(is_storefront: bool) -> str:
    today = timezone.now().date()
    rows = (
        OrderItem.objects
        .filter(order__is_storefront=is_storefront, order__order_date__date=today)
        .values('product_name')
        .annotate(total_qty=Sum('quantity'))
        .order_by('product_name')
    )
    if not rows:
        return "No items to restock today."
    return "\n".join(f"{row['product_name']} x{row['total_qty']}" for row in rows)


@shared_task
def send_third_st_restock():
    send_telegram_message(_build_restock_message(is_storefront=True))


@shared_task
def send_delivery_restock():
    send_telegram_message(_build_restock_message(is_storefront=False))
