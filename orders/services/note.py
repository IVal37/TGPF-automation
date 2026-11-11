# imports from project
from orders.constants import CARD_FEE_MULTIPLIER, PaymentType
from orders.services.pricing import effective_total, round_money
from orders.services.time import format_time
from orders.services.webhook_payload import is_weedmaps_source

# builds order-notes text for WebJoint from an Order model instance.
# reads payment_type/total/eta directly off the order, so the caller is
# responsible for passing an order whose fields reflect the final, approved
# state (e.g. re-fetched from the DB after a dispatcher edits the ETA window).
def get_order_notes(order):
    is_wm = is_weedmaps_source(order.source)
    payment_type = order.payment_type
    debit_total = round_money(effective_total(order) * CARD_FEE_MULTIPLIER)

    if payment_type == PaymentType.CASH:
        first_line = "WM says cash" if is_wm else "cash"
        first_line += f"\nif TTP: ${debit_total}"
    elif payment_type == PaymentType.MERCHANT_PAY:
        first_line = "merchant pay: sent"
    else:
        first_line = f"TTP: ${debit_total}"

    if order.eta_start and order.eta_end:
        eta_line = f"ETA: {format_time(order.eta_start)}-{format_time(order.eta_end)}"
    else:
        eta_line = "ETA: TBD"

    return first_line + "\n" + eta_line
