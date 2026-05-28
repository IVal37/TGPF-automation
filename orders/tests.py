import json
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from django.test import RequestFactory, SimpleTestCase

from orders.services.dispatch import (
    extract_msg_info,
    get_dispatch_msg,
    normal_dispatch_msg,
    under_min_msg,
    get_adjusted_total,
    CalculateForMinCheck,
    populate_items,
)

# ── shared fixture ────────────────────────────────────────────────────────────

SAMPLE_ORDER = {
    "id": 6767008,
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


def _msg(**overrides):
    """Build a msg_dict as extract_msg_info would produce, with optional overrides."""
    base = {
        "id": "6767008",
        "city": "Davis",
        "name": "Hailey",
        "last_name": "White",
        "phone": "15307239356",
        "pay_type": "Cash",
        "discount": Decimal("13.20"),
        "sub_total": Decimal("65.98"),
        "total": Decimal("77.25"),
        "source": "Website",
    }
    base.update(overrides)
    return base


# ── extract_msg_info ──────────────────────────────────────────────────────────

class TestExtractMsgInfo(SimpleTestCase):
    def setUp(self):
        self.result = extract_msg_info(SAMPLE_ORDER)

    def test_id_is_string(self):
        self.assertEqual(self.result["id"], "6767008")
        self.assertIsInstance(self.result["id"], str)

    def test_first_and_last_name(self):
        self.assertEqual(self.result["name"], "Hailey")
        self.assertEqual(self.result["last_name"], "White")

    def test_phone_strips_leading_plus(self):
        self.assertEqual(self.result["phone"], "15307239356")

    def test_payment_type_from_payments_array(self):
        self.assertEqual(self.result["pay_type"], "Cash")

    def test_discount_is_rounded_decimal(self):
        self.assertEqual(self.result["discount"], Decimal("13.20"))
        self.assertIsInstance(self.result["discount"], Decimal)

    def test_total_is_rounded_decimal(self):
        self.assertEqual(self.result["total"], Decimal("77.25"))
        self.assertIsInstance(self.result["total"], Decimal)

    def test_city_from_shipping(self):
        self.assertEqual(self.result["city"], "Davis")

    def test_source_field(self):
        self.assertEqual(self.result["source"], "External")


# ── CalculateForMinCheck ──────────────────────────────────────────────────────

class TestCalculateForMinCheck(SimpleTestCase):
    def test_subtracts_discount_from_subtotal(self):
        self.assertEqual(
            CalculateForMinCheck(_msg(sub_total=Decimal("65.98"), discount=Decimal("13.20"))),
            Decimal("52.78"),
        )

    def test_zero_discount(self):
        self.assertEqual(
            CalculateForMinCheck(_msg(sub_total=50.00, discount=Decimal("0"))),
            Decimal("50.00"),
        )


# ── get_adjusted_total ────────────────────────────────────────────────────────

class TestGetAdjustedTotal(SimpleTestCase):
    def test_cash_unchanged(self):
        self.assertEqual(get_adjusted_total(Decimal("77.25"), "Cash"), Decimal("77.25"))

    def test_card_adds_three_percent(self):
        self.assertEqual(get_adjusted_total(Decimal("100.00"), "Debit / Tap-to-pay"), Decimal("103.00"))

    def test_merchant_pay_ach_adds_fee(self):
        result = get_adjusted_total(Decimal("100.00"), "Merchant Pay - ACH")
        expected = Decimal("100.00") + Decimal("100.00") * Decimal("0.0225") + Decimal("0.35")
        self.assertEqual(result, expected)


# ── populate_items ────────────────────────────────────────────────────────────

class TestPopulateItems(SimpleTestCase):
    def test_formats_name_brand_quantity(self):
        items, _ = populate_items(SAMPLE_ORDER["details"])
        self.assertEqual(items[0], "Raspberry Parfait | All-In-One Rechargeable | BBrand (1)")
        self.assertEqual(items[1], "Melon Fizz | All-In-One Rechargeable | BBrand (1)")

    def test_sums_total_item_count(self):
        _, count = populate_items(SAMPLE_ORDER["details"])
        self.assertEqual(count, 2)

    def test_multi_quantity_counted_correctly(self):
        data = [{"quantity": 3, "variant": {"product": {"name": "X", "brand": {"name": "Y"}}}}]
        items, count = populate_items(data)
        self.assertEqual(count, 3)
        self.assertIn("(3)", items[0])


# ── under_min_msg ─────────────────────────────────────────────────────────────

class TestUnderMinMsg(SimpleTestCase):
    def test_suggests_adding_when_close_to_minimum(self):
        msg = under_min_msg(_msg(city="Woodland"), Decimal("35.00"), Decimal("30.00"))
        self.assertIn("Could we add something", msg)

    def test_cancels_when_far_below_large_minimum(self):
        # Guinda min=250, order=50 → diff=200 > half of 250 → cancel
        msg = under_min_msg(_msg(city="Guinda"), Decimal("250.00"), Decimal("50.00"))
        self.assertIn("cancel", msg)

    def test_dollar_sign_before_order_amount(self):
        msg = under_min_msg(_msg(city="Woodland"), Decimal("35.00"), Decimal("30.00"))
        self.assertIn("$30.00", msg)

    def test_dollar_sign_before_city_minimum(self):
        msg = under_min_msg(_msg(city="Woodland"), Decimal("35.00"), Decimal("30.00"))
        self.assertIn("$35.00", msg)

    def test_greeting_uses_first_name(self):
        msg = under_min_msg(_msg(), Decimal("35.00"), Decimal("30.00"))
        self.assertIn("Hi Hailey", msg)

    def test_city_name_in_body(self):
        msg = under_min_msg(_msg(city="Woodland"), Decimal("35.00"), Decimal("30.00"))
        self.assertIn("Woodland", msg)


# ── normal_dispatch_msg ───────────────────────────────────────────────────────

class TestNormalDispatchMsg(SimpleTestCase):
    def _state(self, driver_city):
        driver = MagicMock()
        driver.get_current_city.return_value = driver_city
        return (
            patch("orders.services.dispatch.driver_id_by_order_id", {"6767008": 1}),
            patch("orders.services.dispatch.drivers_by_id", {1: driver}),
        )

    def test_in_area_cash_message(self):
        p1, p2 = self._state("Davis")
        with p1, p2:
            msg = normal_dispatch_msg(_msg())
        self.assertIn("is in the area", msg)
        self.assertIn("$77.25", msg)
        self.assertNotIn("card fee", msg)

    def test_no_double_space_in_driver_sentence(self):
        p1, p2 = self._state("Davis")
        with p1, p2:
            msg = normal_dispatch_msg(_msg())
        self.assertNotIn("  ", msg)

    def test_not_in_area_shows_eta_window(self):
        p1, p2 = self._state("Woodland")
        with p1, p2, patch("orders.services.dispatch.get_eta", return_value=("2:30", "3:30")):
            msg = normal_dispatch_msg(_msg())
        self.assertIn("between 2:30-3:30", msg)

    def test_debit_card_shows_fee_text_and_adjusted_total(self):
        p1, p2 = self._state("Davis")
        with p1, p2:
            msg = normal_dispatch_msg(_msg(pay_type="Debit / Tap-to-pay"))
        self.assertIn("including card fee", msg)
        self.assertIn("$79.57", msg)  # 77.25 * 1.03 = 79.5675 → rounds to 79.57

    def test_merchant_pay_customer_gets_link_notice_and_fee_text(self):
        p1, p2 = self._state("Davis")
        with p1, p2:
            msg = normal_dispatch_msg(_msg(
                name="Imran", last_name="Rahim", pay_type="Merchant Pay - ACH"
            ))
        self.assertIn("merchant pay link", msg)
        self.assertIn("including Merchant Pay fee", msg)

    def test_non_merchant_pay_customer_has_no_link_notice(self):
        p1, p2 = self._state("Davis")
        with p1, p2:
            msg = normal_dispatch_msg(_msg())
        self.assertNotIn("merchant pay link", msg)

    @patch("orders.services.dispatch.get_wm_payment_type", return_value="Cash")
    def test_external_source_scrapes_payment_type(self, mock_scraper):
        p1, p2 = self._state("Davis")
        with p1, p2:
            normal_dispatch_msg(_msg(source="External"))
        mock_scraper.assert_called_once()

    @patch("orders.services.dispatch.get_wm_payment_type")
    def test_pos_source_does_not_scrape(self, mock_scraper):
        p1, p2 = self._state("Davis")
        with p1, p2:
            normal_dispatch_msg(_msg(source="POS"))
        mock_scraper.assert_not_called()


# ── get_dispatch_msg routing ──────────────────────────────────────────────────

class TestGetDispatchMsgRouting(SimpleTestCase):
    def _state(self):
        driver = MagicMock()
        driver.get_current_city.return_value = "Davis"
        return (
            patch("orders.services.dispatch.driver_id_by_order_id", {"6767008": 1}),
            patch("orders.services.dispatch.drivers_by_id", {1: driver}),
        )

    def test_above_minimum_produces_normal_message(self):
        # Davis min=$25; 65.98 - 13.20 = 52.78 → above minimum
        p1, p2 = self._state()
        with p1, p2:
            msg = get_dispatch_msg(_msg())
        self.assertIn("thank you for your order", msg)
        self.assertNotIn("minimum", msg)

    def test_below_minimum_produces_under_min_message(self):
        # Guinda min=$250; 30.00 - 0 = 30.00 → below minimum
        msg = get_dispatch_msg(_msg(city="Guinda", sub_total=30.00, discount=Decimal("0")))
        self.assertIn("minimum", msg)
        self.assertIn("$250.00", msg)

    def test_exactly_at_minimum_triggers_under_min(self):
        # Davis min=$25; 25.00 is not > 25.00 → under_min path
        msg = get_dispatch_msg(_msg(city="Davis", sub_total=25.00, discount=Decimal("0")))
        self.assertIn("minimum", msg)


# ── new_order view ────────────────────────────────────────────────────────────

class TestNewOrderView(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, payload=None):
        return self.factory.post(
            "/new_order",
            data=json.dumps(payload or SAMPLE_ORDER),
            content_type="application/json",
        )

    def test_returns_200(self):
        from orders.views import new_order
        with patch("orders.views.add_order"), \
             patch("orders.views.Order"), \
             patch("orders.views.extract_msg_info", return_value={"id": "6767008", "phone": "15307239356", "pay_type": "Cash"}), \
             patch("orders.views.get_dispatch_msg", return_value="msg"), \
             patch("orders.views.send_message"):
            response = new_order(self._post())
        self.assertEqual(response.status_code, 200)

    def test_full_chain_is_called(self):
        from orders.views import new_order
        with patch("orders.views.add_order") as mock_add, \
             patch("orders.views.Order"), \
             patch("orders.views.extract_msg_info", return_value={"id": "6767008", "phone": "15307239356", "pay_type": "Cash"}) as mock_extract, \
             patch("orders.views.get_dispatch_msg", return_value="msg") as mock_get_msg, \
             patch("orders.views.send_message") as mock_send:
            new_order(self._post())
        mock_add.assert_called_once()
        mock_extract.assert_called_once()
        mock_get_msg.assert_called_once()
        mock_send.assert_called_once_with("15307239356", "msg")

    def test_add_order_receives_correct_fields(self):
        from orders.views import new_order
        with patch("orders.views.add_order") as mock_add, \
             patch("orders.views.Order"), \
             patch("orders.views.extract_msg_info", return_value={"id": "6767008", "phone": "15307239356", "pay_type": "Cash"}), \
             patch("orders.views.get_dispatch_msg", return_value="msg"), \
             patch("orders.views.send_message"):
            new_order(self._post())
        kw = mock_add.call_args.kwargs
        self.assertEqual(kw["order_id"], "6767008")
        self.assertEqual(kw["order_city"], "Davis")
        self.assertEqual(kw["customer_name"], "Hailey White")
        self.assertEqual(kw["customer_phone"], "15307239356")
        self.assertEqual(kw["source"], "External")
        self.assertEqual(kw["discount"], Decimal("13.2"))
        self.assertEqual(kw["subtotal"], Decimal("65.98"))
        self.assertEqual(kw["total"], Decimal("77.25"))

    def test_order_time_parsed_from_created_field(self):
        """'created_at' does not exist in the payload; key is 'created' with Z suffix."""
        from orders.views import new_order
        with patch("orders.views.add_order") as mock_add, \
             patch("orders.views.Order"), \
             patch("orders.views.extract_msg_info", return_value={"id": "6767008", "phone": "15307239356", "pay_type": "Cash"}), \
             patch("orders.views.get_dispatch_msg", return_value="msg"), \
             patch("orders.views.send_message"):
            response = new_order(self._post())
        self.assertEqual(response.status_code, 200)
        expected = datetime(2025, 12, 7, 19, 26, 23, 124000, tzinfo=timezone.utc)
        self.assertEqual(mock_add.call_args.kwargs["order_time"], expected)

    def test_items_come_from_details_not_items_count(self):
        """data['items'] is 2 (int count); the actual list is under data['details']."""
        from orders.views import new_order
        with patch("orders.views.add_order") as mock_add, \
             patch("orders.views.Order"), \
             patch("orders.views.extract_msg_info", return_value={"id": "6767008", "phone": "15307239356", "pay_type": "Cash"}), \
             patch("orders.views.get_dispatch_msg", return_value="msg"), \
             patch("orders.views.send_message"):
            new_order(self._post())
        items = mock_add.call_args.kwargs["items"]
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 2)
