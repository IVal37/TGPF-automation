# imports from std lib
from decimal import Decimal
from collections import defaultdict
from typing import DefaultDict, Tuple

_DEFAULT_TRAVEL_TIME = 30
_SAME_CITY_TRAVEL_TIME = 15
_DELIVERY_TIME = 5
_BUFFER_TIME = 10

CITY_MINS = {
    "American Canyon": Decimal("125.00"),
    "Brooks": Decimal("200.00"),
    "Davis": Decimal("25.00"),
    "Dixon": Decimal("35.00"),
    "Dunnigon": Decimal("200.00"),
    "East Sacramento": Decimal("75.00"),
    "Elk Grove": Decimal("65.00"),
    "Esparto": Decimal("100.00"),
    "Fairfield": Decimal("55.00"),
    "Guinda": Decimal("250.00"),
    "Knights Landing": Decimal("125.00"),
    "Midtown Sacramento": Decimal("65.00"),
    "Roseville": Decimal("175.00"),
    "Rocklin": Decimal("175.00"),
    "South Sacramento": Decimal("100.00"),
    "Suisun City": Decimal("60.00"),
    "Vacaville": Decimal("45.00"),
    "Vallejo": Decimal("125.00"),
    "West Sacramento": Decimal("55.00"),
    "Winters": Decimal("55.00"),
    "Woodland": Decimal("35.00"),
}

MERCHANT_PAY_CUSTOMERS = (
    "Imran Rahim",
    "Emmi Towns",
    "Chelsea Rosenkild"
)

# returns TRAVEL_TIME dict populated in both directions
def symmetrize(base_dict: DefaultDict[Tuple[str, str], int]) -> DefaultDict:
    symetric_dict: DefaultDict[Tuple[str, str], int] = defaultdict(lambda: _DEFAULT_TRAVEL_TIME, {})

    for (a, b), mins in base_dict.items():
        symetric_dict[(a, b)] = mins
        symetric_dict[(b, a)] = mins
        symetric_dict[(a, a)] = _SAME_CITY_TRAVEL_TIME
        symetric_dict[(b, b)] = _SAME_CITY_TRAVEL_TIME

    return symetric_dict

TRAVEL_TIME: DefaultDict[Tuple[str, str], int] = symmetrize(defaultdict(
    lambda: _DEFAULT_TRAVEL_TIME,
    {
        ("Davis", "Dixon"): 15,
        ("Davis", "Fairfield"): 40,
        ("Davis", "Vacaville"): 25,
        ("Davis", "Winters"): 30,
        ("Davis", "Woodland"): 20,

        ("Dixon", "Fairfield"): 25,
        ("Dixon", "Vacaville"): 15,
        ("Dixon", "Winters"): 20,
        ("Dixon", "Woodland"): 25,

        ("Fairfield", "Vacaville"): 10,
        ("Fairfield", "Winters"): 25,
        ("Fairfield", "Woodland"): 45,
        
        ("Vacaville", "Winters"): 20,
        ("Vacaville", "Woodland"): 30,

        ("Winters", "Woodland"): 30
    }
))

def get_travel_time(city_a: str, city_b: str) -> int:
    if city_a == city_b:
        return _SAME_CITY_TRAVEL_TIME
    return TRAVEL_TIME.get((city_a, city_b), _DEFAULT_TRAVEL_TIME)

BLOCKED_ADDRESSES = {
    ("526 3rd Street", "95618"),
}

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