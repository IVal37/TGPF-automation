# imports from std lib
from datetime import datetime
from decimal import Decimal
from collections import defaultdict
from typing import List, DefaultDict

# imports from project
from orders.models import Order, OrderItem, Driver
from orders.services.state import orders_by_id, driver_id_by_order_id, drivers_by_id, active_drivers, completed_orders

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
    items: list = None,
) -> None:
    if not active_drivers:
        raise ValueError("No active drivers available")
    assigned_driver: Driver = min(active_drivers, key=lambda d: d.get_id())

    order = Order(
        order_id=order_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        city=order_city,
        order_date=order_time,
        source=source,
        discount=discount,
        subtotal=subtotal,
        total=total,
    )
    order.save()

    if items:
        for item in items:
            price = Decimal(str(item.get("totalPrice", item.get("price", "0.00"))))
            OrderItem.objects.create(
                order=order,
                product_name=item["variant"]["product"]["name"],
                quantity=item["quantity"],
                price=price,
            )

    assigned_driver.add_order(order)

    orders_by_id[order_id] = order
    driver_id_by_order_id[order_id] = assigned_driver.get_id()

# complete order and remove from associated data structures
def complete_order(order_id: str) -> None:
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

    Order.objects.filter(order_id=order_id).update(completed=True, driver=None)

    if order_id in driver_id_by_order_id:
        driver_id: int = driver_id_by_order_id.pop(order_id)
        order = orders_by_id.pop(order_id)
        if driver_id in drivers_by_id:
            drivers_by_id[driver_id].set_current_city(order.get_city())

# cancel order and remove from associated data structures (no restock capture)
def cancel_order(order_id: str) -> None:
    Order.objects.filter(order_id=order_id).update(completed=True, driver=None)

    if order_id in driver_id_by_order_id:
        driver_id: int = driver_id_by_order_id.pop(order_id)
        order = orders_by_id.pop(order_id)
        if driver_id in drivers_by_id:
            drivers_by_id[driver_id].set_current_city(order.get_city())

# load a driver from the DB into the active pool
def add_driver(driver_id: int) -> None:
    driver: Driver = Driver.objects.get(pk=driver_id)
    driver.is_active = True
    driver.save()
    active_drivers.append(driver)
    drivers_by_id[driver.get_id()] = driver

# remove driver from the active pool and reassign orders
def remove_driver(driver_id: int) -> None:
    driver = drivers_by_id.pop(driver_id)
    driver.is_active = False
    driver.save()
    for idx, d in enumerate(active_drivers):
        if d.get_id() == driver_id:
            active_drivers.pop(idx)
            break

# permanently delete a driver from memory and the DB
def delete_driver(driver_id: int) -> None:
    drivers_by_id.pop(driver_id, None)
    for idx, d in enumerate(active_drivers):
        if d.get_id() == driver_id:
            active_drivers.pop(idx)
            break
    Driver.objects.filter(pk=driver_id).delete()

# return a dictionary of driver id -> list of driver orders
def get_driver_city_queues(driver_id: int) -> DefaultDict[str, List[int]]:
    order_list: List[int] = [order.get_id() for order in drivers_by_id[driver_id].get_active_orders()]

    orders_by_city: DefaultDict[str, List[int]] = defaultdict(list)
    for order_id in order_list:
        order: Order = orders_by_id[order_id]
        orders_by_city[order.get_city()].append(order_id)

    return orders_by_city
