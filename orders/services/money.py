# imports from std lib
from decimal import Decimal, ROUND_HALF_UP


# rounds money amounts to nearest cent
# @params:
#   amount - amount to be rounded
# @returns:
#   rounded_amount
def round_money(amount):
    rounded_amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return rounded_amount