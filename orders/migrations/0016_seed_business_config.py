from decimal import Decimal

from django.db import migrations

# The values below are copied from the old orders/constants.py (CITY_MINS,
# MERCHANT_PAY_CUSTOMERS, BLOCKED_ADDRESSES, TRAVEL_TIME) and the hard-coded 3rd St
# and no-text checks that used to live in views.new_order, so behavior is identical
# right after migrating. They stay literals here so the migration keeps working after
# the constants are gone. The seed is idempotent (update_or_create).

CITIES = {
    "American Canyon": "125.00",
    "Brooks": "200.00",
    "Davis": "25.00",
    "Dixon": "35.00",
    "Dunnigan": "200.00",
    "East Sacramento": "75.00",
    "Elk Grove": "65.00",
    "Esparto": "100.00",
    "Fairfield": "55.00",
    "Guinda": "250.00",
    "Knights Landing": "125.00",
    "Midtown Sacramento": "65.00",
    "Roseville": "175.00",
    "Rocklin": "175.00",
    "South Sacramento": "100.00",
    "Suisun City": "60.00",
    "Vacaville": "45.00",
    "Vallejo": "125.00",
    "West Sacramento": "55.00",
    "Winters": "55.00",
    "Woodland": "35.00",
}

MERCHANT_PAY_CUSTOMERS = ("Imran Rahim", "Emmi Towns", "Chelsea Rosenkild")
NO_TEXT_CUSTOMERS = ("Mohsen Awara",)

# (number, street_contains, city, zip, kind)
SPECIAL_ADDRESSES = (
    # the storefront: 526 + "3rd"/"third" + Davis, any zip
    ("526", "3rd", "Davis", "", "storefront"),
    ("526", "third", "Davis", "", "storefront"),
    # the old BLOCKED_ADDRESSES entry ("526 3rd Street", "95616"); a storefront
    # match is always checked first, as the old code skipped it for the storefront
    ("526", "3rd Street", "", "95616", "blocked"),
)

TRAVEL_MINUTES = {
    ("Davis", "Dixon"): 15,
    ("Davis", "Fairfield"): 40,
    ("Davis", "Vacaville"): 35,
    ("Davis", "Winters"): 30,
    ("Davis", "Woodland"): 20,
    ("Dixon", "Fairfield"): 25,
    ("Dixon", "Vacaville"): 20,
    ("Dixon", "Winters"): 20,
    ("Dixon", "Woodland"): 25,
    ("Fairfield", "Vacaville"): 15,
    ("Fairfield", "Winters"): 25,
    ("Fairfield", "Woodland"): 45,
    ("Vacaville", "Winters"): 25,
    ("Vacaville", "Woodland"): 30,
    ("Winters", "Woodland"): 30,
}


def seed(apps, schema_editor):
    City = apps.get_model("orders", "City")
    CustomerFlag = apps.get_model("orders", "CustomerFlag")
    SpecialAddress = apps.get_model("orders", "SpecialAddress")
    TravelTime = apps.get_model("orders", "TravelTime")

    for name, minimum in CITIES.items():
        City.objects.update_or_create(name=name, defaults={"minimum": Decimal(minimum)})
    for name in MERCHANT_PAY_CUSTOMERS:
        CustomerFlag.objects.update_or_create(name=name, defaults={"merchant_pay": True})
    for name in NO_TEXT_CUSTOMERS:
        CustomerFlag.objects.update_or_create(name=name, defaults={"no_text": True})
    for number, street, city, zip_code, kind in SPECIAL_ADDRESSES:
        SpecialAddress.objects.update_or_create(
            number=number, street_contains=street, city=city, zip=zip_code, defaults={"kind": kind},
        )
    for (city_a, city_b), minutes in TRAVEL_MINUTES.items():
        a, b = sorted((city_a, city_b))
        TravelTime.objects.update_or_create(
            city_a=a, city_b=b, defaults={"minutes": minutes, "source": "static", "fetched_at": None},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0015_business_config_models"),
    ]

    # reverse is a no-op: reversing 0015 drops the tables
    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
