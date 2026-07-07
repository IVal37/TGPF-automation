# imports from std lib
from decimal import Decimal

# imports from project
from orders.services.money import round_money
from orders.services.state import orders_by_id
from orders.services.time import format_time

def get_order_notes(dict):
    is_wm = dict["source"] not in ("POS", "Website")
    payment_type = dict["pay_type"]
    debit_total = round_money(dict["total"] * Decimal("1.03"))

    if payment_type == "Cash":
        first_line = "WM says cash" if is_wm else "cash"
    elif payment_type == "Merchant Pay - ACH":
        first_line = "merchant pay: sent"
    else:
        first_line = f"TTP: ${debit_total}"

    order = orders_by_id[dict["id"]]
    if order.eta_start and order.eta_end:
        eta_line = f"ETA: {format_time(order.eta_start)}-{format_time(order.eta_end)}"
    else:
        eta_line = "ETA: TBD"

    return first_line + "\n" + eta_line
