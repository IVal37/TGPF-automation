# imports from std lib
from decimal import Decimal

# imports from project
from orders.services.dispatch import get_effective_total
from orders.services.money import round_money
from orders.services.time import format_time

# builds order-notes text for WebJoint from an Order model instance.
# reads payment_type/total/eta directly off the order, so the caller is
# responsible for passing an order whose fields reflect the final, approved
# state (e.g. re-fetched from the DB after a dispatcher edits the ETA window).
def get_order_notes(order):
    is_wm = order.source not in ("POS", "Website")
    payment_type = order.payment_type
    debit_total = round_money(get_effective_total(order) * Decimal("1.03"))

    if payment_type == "Cash":
        first_line = "WM says cash" if is_wm else "cash"
        first_line += f"\nif TTP: ${debit_total}"
    elif payment_type == "Merchant Pay - ACH":
        first_line = "merchant pay: sent"
    else:
        first_line = f"TTP: ${debit_total}"

    if order.eta_start and order.eta_end:
        eta_line = f"ETA: {format_time(order.eta_start)}-{format_time(order.eta_end)}"
    else:
        eta_line = "ETA: TBD"

    return first_line + "\n" + eta_line
