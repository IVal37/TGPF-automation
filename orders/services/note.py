# imports from std lib
from decimal import Decimal

# imports from project
from orders.services.scrapers.weedmaps import get_wm_payment_type
from orders.services.money import round_money
from orders.services.state import orders_by_id

def get_order_notes(dict):
    payment_type = (
        dict["pay_type"]
        if (dict["source"]) in ("POS", "Website")
        else get_wm_payment_type()
    )
    debit_total = dict["total"] * Decimal("1.03")
    rounded_debit_total = round_money(debit_total)

    if payment_type == "Cash":
        first_line = f"Cash (if TTP: ${rounded_debit_total})"
    elif payment_type == "Merchant Pay - ACH":
        first_line = "merchant pay: sent"
    else:
        first_line = f"TTP: ${rounded_debit_total}"

    order = orders_by_id[dict["id"]]

    order_notes = first_line + "\n" + f"ETA: {order}"

    return order_notes
