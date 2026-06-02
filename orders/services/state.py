# imports from std lib
from typing import Dict, List

# imports from project
from orders.models import Order, Driver

orders_by_id: Dict[str, Order] = {}
driver_id_by_order_id: Dict[str, int] = {}
drivers_by_id: Dict[int, Driver] = {}
active_drivers: List[Driver] = []

# completed orders pending restock review: list of {order_id, customer_name, items}
completed_orders: List[dict] = []

# aggregated restock list: product_name -> {quantity, price}
restock_items: Dict[str, dict] = {}
