# imports from std lib
from decimal import Decimal

# Business config that varies by city, customer, address or route (city minimums,
# merchant-pay / no-text customers, blocked and storefront addresses, travel times)
# lives in the database: see orders/services/config.py and orders/services/travel.py,
# and edit it in Django admin.

# ── Payment types (the labels WebJoint and our own code use) ─────────────────
class PaymentType:
    CASH = "Cash"
    MERCHANT_PAY = "Merchant Pay - ACH"
    DEBIT = "Debit / Tap-to-pay"


# ── Places ───────────────────────────────────────────────────────────────────
# where a driver is assumed to start the day
HOME_CITY = "Davis"
# the "city" stored on walk-in storefront sales
STOREFRONT_CITY = "3rd St"

# ── Fees and minimums ────────────────────────────────────────────────────────
# merchant-pay (ACH) orders pay 2.25% plus 35 cents on top of the total
MERCHANT_PAY_RATE = Decimal("0.0225")
MERCHANT_PAY_FLAT_FEE = Decimal("0.35")
# a city minimum above this, with an order under half of it, gets a "cancel" text
# instead of asking the customer to add to the order
BIG_MINIMUM = Decimal("100.00")

# ── ETA estimates (orders/services/eta.py) ───────────────────────────────────
# minutes a driver spends at each stop
DELIVERY_MINUTES = 5
# an ETA is never sooner than this, or later than MAX_LEAD, after the order was placed
MIN_LEAD_MINUTES = 30
MAX_LEAD_MINUTES = 120
# length of the ETA window; busier days (more than BUSY_ORDER_COUNT open orders) get the longer one
WINDOW_MINUTES = 60
BUSY_WINDOW_MINUTES = 90
BUSY_ORDER_COUNT = 5

# card / tap-to-pay orders are charged a 3% fee on top of the total
CARD_FEE_MULTIPLIER = Decimal("1.03")

# order sources that come straight from our own channels; anything else
# (e.g. "External") is a Weedmaps order
DIRECT_SOURCES = ("POS", "Website")

# placeholder accounts used for a second adult on a legal-limit split order,
# or (unrelated to that) a lazy walk-in sale at 3rd St — see dispatch.is_fsa_fsm
FSA_FSM_NAMES = {"frank sinatra adult", "frank sinatra medical"}

VALEDICTIONS = [
                    'Thanks again for your order!',
                    'We appreciate your business!',
                    'Have a great day!',
                    'We hope you enjoy your order!',
                    'Thanks for choosing TGPF!',
                    'Thanks!',
                    'Enjoy!',
                    'Have a good one!',
                ]
