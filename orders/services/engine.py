# imports from std lib
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

# imports from third party
from django.utils import timezone

# imports from project
from orders.constants import STOREFRONT_CITY
from orders.models import QUEUE_STATUSES, Driver, Order, OrderItem, OrderStatus
from orders.services import queries
from orders.services.eta import estimate_window
from orders.services.travel import get_travel_time

logger = logging.getLogger(__name__)


# pick the active driver best suited to take on a stop in order_city:
# least-loaded first (fewest current orders), then closest to order_city,
# then lowest id for determinism when still tied
# returns None when nobody is on shift
def _pick_driver(order_city: str) -> Optional[Driver]:
    drivers = list(queries.active_drivers())
    if not drivers:
        return None
    order_counts = queries.open_order_counts()

    def sort_key(d: Driver):
        return (order_counts.get(d.pk, 0), get_travel_time(d.current_city, order_city), d.pk)

    return min(drivers, key=sort_key)


# persist a webhook's "details" line items against an already-saved order
def _save_items(order: Order, items: list) -> None:
    for item in items or []:
        price = Decimal(str(item.get("totalPrice", item.get("price", "0.00"))))
        OrderItem.objects.create(
            order=order,
            product_name=item["variant"]["product"]["name"],
            quantity=item["quantity"],
            price=price,
        )


# mark an order finished in the DB (used for completes, cancels, and shells)
def _close_order_in_db(order_id: str, status: str = Order.Status.COMPLETED) -> None:
    Order.objects.filter(order_id=order_id).update(status=status, driver=None)


# a driver who is no longer on shift keeps their last city untouched
def _move_driver(driver_id: int, city: str) -> None:
    driver = Driver.objects.filter(pk=driver_id, is_active=True).first()
    if driver is not None:
        driver.set_current_city(city)


# add order and add to associated data structures
def add_order(
    order_city: str,
    order_id: str,
    customer_name: str,
    order_time: datetime,
    customer_phone: str = "",
    source: str = "",
    discount: Decimal = Decimal("0"),
    subtotal: Decimal = Decimal("0"),
    total: Decimal = Decimal("0"),
    delivery_fee: Decimal = Decimal("0"),
    items: list = None,
    webjoint_id: str = "",
    weedmaps_order_id: str = "",
    status: str = Order.Status.AWAITING_APPROVAL,
    scheduled_start: Optional[datetime] = None,
    scheduled_end: Optional[datetime] = None,
) -> Order:
    # with nobody on shift the order is still saved (driver=None); a dispatcher
    # assigns it later with assign_order, which is when its ETA is computed
    assigned_driver: Optional[Driver] = _pick_driver(order_city)

    order = Order(
        order_id=order_id,
        webjoint_id=webjoint_id,
        weedmaps_order_id=weedmaps_order_id,
        status=status,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        customer_name=customer_name,
        customer_phone=customer_phone,
        city=order_city,
        order_date=order_time,
        source=source,
        discount=discount,
        subtotal=subtotal,
        total=total,
        delivery_fee=delivery_fee,
    )
    order.save()
    _save_items(order, items)
    if assigned_driver is not None:
        assigned_driver.add_order(order)
    else:
        logger.warning("add_order %s: no driver on shift; saved unassigned", order_id)
    return order


# hand an unassigned order to a driver. A queued order gets its ETA now (and the
# customer's scheduled window, if they picked one); an order that was already
# texted keeps the ETA the customer was told.
def assign_order(order_id: str, driver_id: int) -> None:
    order = Order.objects.get(order_id=order_id)
    driver = Driver.objects.get(pk=driver_id, is_active=True)
    driver.add_order(order)

    if order.status in QUEUE_STATUSES:
        estimate_window(order_id)


# record a walk-in storefront sale (e.g. 3rd St pickup): no driver, no ETA, no
# dispatch SMS — just persisted so the nightly restock digest can query it.
# Storefront sales are complete the moment they're rung up, so mark them done
# immediately rather than routing them through complete_order later.
def add_storefront_order(
    order_id: str,
    customer_name: str,
    order_time: datetime,
    source: str = "",
    discount: Decimal = Decimal("0"),
    subtotal: Decimal = Decimal("0"),
    total: Decimal = Decimal("0"),
    delivery_fee: Decimal = Decimal("0"),
    items: list = None,
) -> None:
    order = Order(
        order_id=order_id,
        customer_name=customer_name,
        city=STOREFRONT_CITY,
        order_date=order_time,
        source=source,
        discount=discount,
        subtotal=subtotal,
        total=total,
        delivery_fee=delivery_fee,
        status=Order.Status.STOREFRONT,
    )
    order.save()
    _save_items(order, items)


# complete an order. If it was out with a driver today, that driver is now in the
# order's city, and one of their placeholder shells there is done too.
def complete_order(order_id: str, city: str = "") -> None:
    tracked = queries.open_orders().filter(order_id=order_id).first()
    existing = Order.objects.filter(order_id=order_id).first()
    already_closed = existing is not None and existing.status in (
        OrderStatus.COMPLETED,
        OrderStatus.CANCELLED,
        OrderStatus.STOREFRONT,
    )

    _close_order_in_db(order_id)

    if tracked is not None:
        city = tracked.city
        _move_driver(tracked.driver_id, city)

        shell = queries.find_open_shell(city, tracked.driver_id)
        if shell is not None:
            _close_order_in_db(shell.order_id)

    elif city and not already_closed:
        # order wasn't tracked (placed before today, or not ours) - pop a
        # matching shell. A repeat webhook for an order that is already closed
        # must not move a driver or close another shell a second time.
        shell = queries.find_open_shell(city)
        if shell is not None:
            _close_order_in_db(shell.order_id)
            _move_driver(shell.driver_id, city)


# add a shell order (city only) so driver location updates when it's completed
def add_shell_order(city: str) -> None:
    assigned_driver = _pick_driver(city)
    if assigned_driver is None:
        raise ValueError("No active drivers available")

    order_id = f"shell_{uuid.uuid4().hex}"
    order = Order(
        order_id=order_id,
        city=city,
        order_date=timezone.now(),
        is_shell=True,
        # a shell is with a driver and never queued
        status=Order.Status.SENT,
    )
    order.save()
    assigned_driver.add_order(order)


# cancel an order (no restock capture); its driver ends up in the order's city
def cancel_order(order_id: str) -> None:
    tracked = queries.open_orders().filter(order_id=order_id).first()

    _close_order_in_db(order_id, OrderStatus.CANCELLED)

    if tracked is not None:
        _move_driver(tracked.driver_id, tracked.city)


# put a driver on shift
def add_driver(driver_id: int) -> None:
    driver: Driver = Driver.objects.get(pk=driver_id)
    driver.is_active = True
    driver.save(update_fields=["is_active"])


# take a driver off shift. Their open orders (already-texted ones too) become
# unassigned so a dispatcher can hand them to someone else; their placeholder
# shells only mark where they were heading, so they are closed.
def remove_driver(driver_id: int) -> None:
    open_orders = queries.open_orders(driver_id)
    shell_ids = list(open_orders.filter(is_shell=True).values_list("order_id", flat=True))
    order_pks = list(open_orders.filter(is_shell=False).values_list("pk", flat=True))
    for shell_id in shell_ids:
        _close_order_in_db(shell_id)
    Order.objects.filter(pk__in=order_pks).update(driver=None)
    Driver.objects.filter(pk=driver_id).update(is_active=False)


# permanently delete a driver (their open orders are unassigned first, not lost)
def delete_driver(driver_id: int) -> None:
    remove_driver(driver_id)
    Driver.objects.filter(pk=driver_id).delete()
