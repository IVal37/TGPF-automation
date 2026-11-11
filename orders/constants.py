# imports from std lib
from decimal import Decimal

# Business config that varies by city, customer, address or route (city minimums,
# merchant-pay / no-text customers, blocked and storefront addresses, travel times)
# lives in the database: see orders/services/config.py and orders/services/travel.py,
# and edit it in Django admin.

_DELIVERY_TIME = 5

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