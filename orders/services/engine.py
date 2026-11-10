# imports from std lib
from datetime import datetime
from decimal import Decimal
from collections import Counter, defaultdict
from typing import List, DefaultDict, Optional
import uuid

# imports from project
from orders.models import Order, OrderItem, Driver
from orders.services.state import orders_by_id, driver_id_by_order_id, drivers_by_id, active_drivers, completed_orders
from orders.constants import get_travel_time

# pick the active driver best suited to take on a stop in order_city:
# least-loaded first (fewest current orders), then closest to order_city,
# then lowest id for determinism when still tied
def _pick_driver(order_city: str) -> Driver:
    if not active_drivers:
        raise ValueError("No active drivers available")
    order_counts = Counter(driver_id_by_order_id.values())

    def sort_key(d: Driver):
        return (order_counts.get(d.get_id(), 0), get_travel_time(d.get_current_city(), order_city), d.get_id())

    return min(active_drivers, key=sort_key)

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

# hand an already-saved order to a driver and start tracking it in memory
def _assign_order(order: Order, driver: Driver) -> None:
    driver.add_order(order)
    orders_by_id[order.order_id] = order
    driver_id_by_order_id[order.order_id] = driver.get_id()

# stop tracking an order in memory; returns the driver it was assigned to
def _untrack_order(order_id: str) -> int:
    orders_by_id.pop(order_id)
    return driver_id_by_order_id.pop(order_id)

# mark an order finished in the DB (used for completes, cancels, and shells).
# status is the source of truth; `completed` is kept written (True for both
# completed and cancelled, as before) for one release and never read.
def _close_order_in_db(order_id: str, status: str = Order.Status.COMPLETED) -> None:
    Order.objects.filter(order_id=order_id).update(status=status, completed=True, driver=None)

def _move_driver(driver_id: int, city: str) -> None:
    if driver_id in drivers_by_id:
        drivers_by_id[driver_id].set_current_city(city)

# first tracked shell order in `city`, optionally only among one driver's orders
def _find_shell(city: str, driver_id: Optional[int] = None) -> Optional[str]:
    return next(
        (oid for oid in list(orders_by_id)
         if orders_by_id[oid].is_shell and orders_by_id[oid].get_city() == city
         and (driver_id is None or driver_id_by_order_id.get(oid) == driver_id)),
        None
    )

def _drop_from_active_pool(driver_id: int) -> None:
    for idx, d in enumerate(active_drivers):
        if d.get_id() == driver_id:
            active_drivers.pop(idx)
            break

# put a Driver instance into the in-memory active pool (no DB write)
def track_active_driver(driver: Driver) -> None:
    active_drivers.append(driver)
    drivers_by_id[driver.get_id()] = driver

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
) -> None:
    assigned_driver: Driver = _pick_driver(order_city)

    order = Order(
        order_id=order_id,
        webjoint_id=webjoint_id,
        weedmaps_order_id=weedmaps_order_id,
        status=status,
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
    _assign_order(order, assigned_driver)

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
        city="3rd St",
        order_date=order_time,
        source=source,
        discount=discount,
        subtotal=subtotal,
        total=total,
        delivery_fee=delivery_fee,
        status=Order.Status.STOREFRONT,
        # kept in step with status for one release; never read
        completed=True,
        dispatch_sent=True,
        is_storefront=True,
    )
    order.save()
    _save_items(order, items)

# complete order and remove from associated data structures
def complete_order(order_id: str, city: str = "") -> None:
    if order_id in orders_by_id:
        order = orders_by_id[order_id]
        completed_orders.append({
            "order_id": order_id,
            "customer_name": order.customer_name,
            "items": [
                {"product_name": item.product_name, "quantity": item.quantity, "price": str(item.price)}
                for item in order.items.all()
            ],
        })

    _close_order_in_db(order_id)

    if order_id in driver_id_by_order_id:
        city = orders_by_id[order_id].get_city()
        driver_id = _untrack_order(order_id)
        _move_driver(driver_id, city)

        # the driver has now been to this city, so one of their placeholder
        # shells there is done too
        shell_id = _find_shell(city, driver_id)
        if shell_id:
            _untrack_order(shell_id)
            _close_order_in_db(shell_id)

    elif city:
        # order wasn't tracked (placed before server started) — pop matching shell
        shell_id = _find_shell(city)
        if shell_id:
            driver_id = _untrack_order(shell_id)
            _close_order_in_db(shell_id)
            _move_driver(driver_id, city)

# add a shell order (city only) so driver location updates when it's completed
def add_shell_order(city: str) -> None:
    assigned_driver: Driver = _pick_driver(city)

    order_id = f"shell_{uuid.uuid4().hex}"
    order = Order(
        order_id=order_id,
        city=city,
        order_date=datetime.now(),
        is_shell=True,
        # a shell is with a driver and never queued
        status=Order.Status.SENT,
        dispatch_sent=True,
    )
    order.save()
    _assign_order(order, assigned_driver)

# cancel order and remove from associated data structures (no restock capture)
def cancel_order(order_id: str) -> None:
    _close_order_in_db(order_id, Order.Status.CANCELLED)

    if order_id in driver_id_by_order_id:
        city = orders_by_id[order_id].get_city()
        _move_driver(_untrack_order(order_id), city)

# load a driver from the DB into the active pool
def add_driver(driver_id: int) -> None:
    driver: Driver = Driver.objects.get(pk=driver_id)
    driver.is_active = True
    driver.save()
    track_active_driver(driver)

# remove driver from the active pool and reassign orders
def remove_driver(driver_id: int) -> None:
    driver = drivers_by_id.pop(driver_id)
    driver.is_active = False
    driver.save()
    _drop_from_active_pool(driver_id)

# permanently delete a driver from memory and the DB
def delete_driver(driver_id: int) -> None:
    drivers_by_id.pop(driver_id, None)
    _drop_from_active_pool(driver_id)
    Driver.objects.filter(pk=driver_id).delete()

# return a dictionary of driver id -> list of driver orders
def get_driver_city_queues(driver_id: int) -> DefaultDict[str, List[str]]:
    orders_by_city: DefaultDict[str, List[str]] = defaultdict(list)
    for order_id, did in driver_id_by_order_id.items():
        if did == driver_id:
            orders_by_city[orders_by_id[order_id].get_city()].append(order_id)
    return orders_by_city
