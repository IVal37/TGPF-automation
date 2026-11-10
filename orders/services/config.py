# imports from std lib
from decimal import Decimal
from typing import List, Optional

# imports from project
from orders.models import City, CustomerFlag, SpecialAddress

# Business config that used to be hard-coded in constants.py and views.py. It lives
# in the database (edit it in Django admin at /admin/), so the web server and the
# Celery worker always agree and a change needs no deploy.


# the order minimum for a city; a city we don't list has none
def city_min(city: str) -> Decimal:
    row = City.objects.filter(name=city).first()
    return row.minimum if row is not None else Decimal("0.00")


# every city we deliver to, alphabetical (for the dashboard dropdowns)
def cities() -> List[str]:
    return sorted(City.objects.values_list("name", flat=True))


def _flagged(full_name: str, **flag) -> bool:
    name = " ".join(full_name.split())
    return bool(name) and CustomerFlag.objects.filter(name__iexact=name, **flag).exists()


# is "<name> <last_name>" a customer who gets the merchant-pay link?
def is_merchant_pay_customer(name: str, last_name: str) -> bool:
    return _flagged(f"{name} {last_name}", merchant_pay=True)


# is this customer one we never text?
def is_no_text_customer(customer_name: str) -> bool:
    return _flagged(customer_name, no_text=True)


# Does a webhook's "shipping" block match a special address? Returns "storefront",
# "blocked" or None. Storefront rows are checked first: the store's own address must
# never be treated as a blocked one.
def match_special_address(shipping: dict) -> Optional[str]:
    number = (shipping.get("number") or "").strip()
    street = (shipping.get("street") or "").strip().lower()
    city = (shipping.get("city") or "").strip().lower()
    zip_code = (shipping.get("zip") or "").strip()

    def matches(row: SpecialAddress) -> bool:
        return (
            row.number == number
            and row.street_contains.strip().lower() in street
            and (not row.city or row.city.strip().lower() == city)
            and (not row.zip or row.zip.strip() == zip_code)
        )

    rows = list(SpecialAddress.objects.all())
    for kind in (SpecialAddress.Kind.STOREFRONT, SpecialAddress.Kind.BLOCKED):
        if any(row.kind == kind and matches(row) for row in rows):
            return kind.value
    return None
