# imports from std lib
from collections import defaultdict
from typing import DefaultDict, Dict, Iterable, List, Optional

# imports from third party
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

# imports from project
from orders.models import OPEN_STATUSES, QUEUE_STATUSES, Driver, Order, OrderStatus

# The database is the only source of truth for who is driving where. Everything
# that used to read the in-memory dicts goes through these helpers.
#
# "Open" means: still queued or out with a driver (OPEN_STATUSES, which covers
# open shells), assigned to a driver, and placed today. The same-day cut-off
# matches what a daily restart used to do, so an order that never got a
# complete/cancel webhook can't inflate routes or driver load forever.


def today():
    return timezone.now().date()


# drivers currently on shift
def active_drivers() -> QuerySet:
    return Driver.objects.filter(is_active=True).order_by("pk")


# today's open orders that have a driver, oldest first (pk order is the order
# they were added, which is the order they were routed in); optionally one
# driver's only
def open_orders(driver_id: Optional[int] = None) -> QuerySet:
    orders = Order.objects.filter(
        status__in=OPEN_STATUSES, driver__isnull=False, order_date__date=today(),
    )
    if driver_id is not None:
        orders = orders.filter(driver_id=driver_id)
    return orders.order_by("pk")


# open orders with no driver: they arrived while nobody was on shift, or their
# driver was switched off. Shells are placeholders for a driver, never unassigned
def unassigned_orders() -> QuerySet:
    return Order.objects.filter(
        status__in=OPEN_STATUSES, driver__isnull=True, is_shell=False, order_date__date=today(),
    ).order_by("pk")


# a driver's orders that were texted and are out for delivery
def out_for_delivery(driver_id: int) -> QuerySet:
    return open_orders(driver_id).filter(status=Order.Status.SENT, is_shell=False)


# {driver_id: number of open orders}, shells included (they are real stops)
def open_order_counts() -> Dict[int, int]:
    # order_by() clears open_orders's pk ordering, which would otherwise split the groups
    rows = open_orders().order_by().values("driver_id").annotate(n=Count("pk"))
    return {row["driver_id"]: row["n"] for row in rows}


# open orders across all drivers, shells excluded: drives the ETA window size
def open_real_order_count() -> int:
    return open_orders().filter(is_shell=False).count()


# first open shell in `city`, optionally only among one driver's orders
def find_open_shell(city: str, driver_id: Optional[int] = None) -> Optional[Order]:
    return open_orders(driver_id).filter(is_shell=True, city=city).first()


# {order_id: city} for the given order ids
def cities_by_order_id(order_ids: Iterable[str]) -> Dict[str, str]:
    return dict(Order.objects.filter(order_id__in=list(order_ids)).values_list("order_id", "city"))


# {city: [order ids of the driver's open orders there, in route order]}
def get_driver_city_queues(driver_id: int) -> DefaultDict[str, List[str]]:
    orders_by_city: DefaultDict[str, List[str]] = defaultdict(list)
    for order in open_orders(driver_id):
        orders_by_city[order.city].append(order.order_id)
    return orders_by_city


# the Dispatch Queue tab: queued orders with an ETA window to approve, today's
# unassigned orders (no driver yet, so no ETA) waiting for a dispatcher to pick one,
# anything being texted right now, and anything with a failure to show (even an order
# already sent: a note that failed to save still needs a look). Shells never show.
def pending_orders() -> QuerySet:
    queued = Q(status__in=QUEUE_STATUSES, eta_start__isnull=False)
    unassigned = Q(status__in=OPEN_STATUSES, driver__isnull=True, order_date__date=today())
    sending = Q(status=OrderStatus.SENDING, order_date__date=today())
    failed = ~Q(last_error="")
    return Order.objects.filter(queued | unassigned | sending | failed, is_shell=False)
