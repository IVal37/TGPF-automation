from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from orders.services.webhook_payload import InvalidPayload, parse_new_order
from orders.tests.helpers import SAMPLE_ORDER, WJ_ID


class TestParseNewOrder(SimpleTestCase):
    def setUp(self):
        self.result = parse_new_order(SAMPLE_ORDER)

    def test_id_is_string(self):
        self.assertEqual(self.result.order_id, "6767008")
        self.assertIsInstance(self.result.order_id, str)

    def test_customer_name(self):
        self.assertEqual(self.result.customer_name, "Hailey White")

    def test_phone_strips_leading_plus(self):
        self.assertEqual(self.result.phone, "15307239356")

    def test_payment_type_from_payments_array(self):
        self.assertEqual(self.result.payment_type, "Cash")

    def test_money_fields_are_decimals(self):
        self.assertEqual(self.result.discount, Decimal("13.2"))
        self.assertEqual(self.result.subtotal, Decimal("65.98"))
        self.assertEqual(self.result.total, Decimal("77.25"))
        self.assertEqual(self.result.delivery_fee, Decimal("0"))

    def test_city_source_and_ids(self):
        self.assertEqual(self.result.city, "Davis")
        self.assertEqual(self.result.source, "External")
        self.assertEqual(self.result.webjoint_id, WJ_ID)
        self.assertEqual(self.result.weedmaps_order_id, "49018286")

    def test_items_come_from_details(self):
        self.assertEqual(len(self.result.items), 2)

    def test_not_scheduled_by_default(self):
        self.assertIsNone(self.result.scheduled_start)
        self.assertIsNone(self.result.scheduled_end)
        self.assertFalse(self.result.is_pending)

    def test_scheduled_window_is_parsed(self):
        cart = {
            "deliveryTiming": "Scheduled",
            "scheduledStart": "2025-12-08T18:00:00Z",
            "scheduledEnd": "2025-12-08T19:00:00Z",
        }
        result = parse_new_order(dict(SAMPLE_ORDER, cart=cart, status="Pending"))
        self.assertEqual(result.scheduled_end - result.scheduled_start, timedelta(hours=1))
        self.assertTrue(result.is_pending)

    def test_asap_cart_has_no_scheduled_window(self):
        cart = {
            "deliveryTiming": "ASAP",
            "scheduledStart": "2025-12-08T18:00:00Z",
            "scheduledEnd": "2025-12-08T19:00:00Z",
        }
        self.assertIsNone(parse_new_order(dict(SAMPLE_ORDER, cart=cart)).scheduled_start)

    def test_a_storefront_style_body_without_phone_or_city_still_parses(self):
        body = {k: v for k, v in SAMPLE_ORDER.items() if k not in ("shipping",)}
        body["customer"] = {"name": "Walk In"}
        result = parse_new_order(body)
        self.assertEqual((result.city, result.phone), ("", ""))
        with self.assertRaises(InvalidPayload):
            result.require_delivery_fields()

    def test_missing_or_unreadable_fields_are_rejected(self):
        def without(key):
            return {k: v for k, v in SAMPLE_ORDER.items() if k != key}

        bad_bodies = {
            "no id": without("id"),
            "no created": without("created"),
            "no customer": without("customer"),
            "no customer name": dict(SAMPLE_ORDER, customer={"phone": "+1530"}),
            "bad timestamp": dict(SAMPLE_ORDER, created="yesterday"),
            "bad number": dict(SAMPLE_ORDER, total="lots"),
            "not an object": ["a list"],
        }
        for label, body in bad_bodies.items():
            with self.subTest(label), self.assertRaises(InvalidPayload):
                parse_new_order(body)

    def test_a_null_payment_method_does_not_crash(self):
        body = dict(SAMPLE_ORDER, payments=[{"paymentMethod": None}])
        self.assertEqual(parse_new_order(body).payment_type, "")
