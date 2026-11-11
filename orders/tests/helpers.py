"""Shared test fixtures: a sample WebJoint webhook and base classes."""

from datetime import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from orders.models import Driver
from orders.services.engine import add_driver, add_order


def as_dispatcher(request):
    """The dashboard views are @login_required; give a RequestFactory request a
    logged-in user (an unsaved User counts as authenticated, so no DB is needed)."""
    request.user = User(username="dispatcher")
    return request


# ── shared fixture ────────────────────────────────────────────────────────────

SAMPLE_ORDER = {
    "id": 6767008,
    "_id": "1111-2222-3333333-6767008",
    "weedmapsOrderId": 49018286,
    "source": "External",
    "created": "2025-12-07T19:26:23.124Z",
    "subtotal": 65.98,
    "discount": 13.2,
    "total": 77.25,
    "phone": "+15307239356",
    "customer": {
        "name": "Hailey White",
        "phone": "+15307239356",
    },
    "shipping": {"city": "Davis"},
    "payments": [{"paymentMethod": {"label": "Cash"}}],
    "details": [
        {
            "quantity": 1,
            "price": 32.99,
            "variant": {
                "product": {
                    "name": "Raspberry Parfait | All-In-One Rechargeable",
                    "brand": {"name": "BBrand"},
                }
            },
        },
        {
            "quantity": 1,
            "price": 32.99,
            "variant": {
                "product": {
                    "name": "Melon Fizz | All-In-One Rechargeable",
                    "brand": {"name": "BBrand"},
                }
            },
        },
    ],
}


WJ_ID = SAMPLE_ORDER["_id"]
# the same order as a direct (point-of-sale) sale: carries its own payment label
POS_ORDER = {k: v for k, v in SAMPLE_ORDER.items() if k != "weedmapsOrderId"}
POS_ORDER["source"] = "POS"


class DbTruthCase(TestCase):
    """TestGetEta's clock: 'today' is a fixed Monday 2pm, so open-order queries are deterministic."""

    BASE = datetime(2026, 7, 20, 14, 0, 0)

    def setUp(self):
        patcher = patch("django.utils.timezone.now", return_value=self.BASE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _driver(self, name, city="Davis"):
        driver = Driver.objects.create(name=name, current_city=city)
        add_driver(driver.pk)
        return driver

    def _add(self, order_id, city, when=None, **kw):
        add_order(order_city=city, order_id=order_id, customer_name="Test", order_time=when or self.BASE, **kw)
