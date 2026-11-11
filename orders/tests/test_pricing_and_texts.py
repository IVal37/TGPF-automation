from datetime import datetime
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from orders.models import Order
from orders.services.customer_texts import render_dispatch_text, under_min_text
from orders.services.customers import is_fsa_fsm
from orders.services.pricing import adjusted_total, effective_total, is_under_min, min_check_total


class TestMinCheckTotal(SimpleTestCase):
    def test_subtracts_discount_from_subtotal(self):
        self.assertEqual(min_check_total(Decimal("65.98"), Decimal("13.20")), Decimal("52.78"))

    def test_zero_discount(self):
        self.assertEqual(min_check_total(50.00, Decimal("0")), Decimal("50.00"))


class TestAdjustedTotal(SimpleTestCase):
    def test_cash_unchanged(self):
        self.assertEqual(adjusted_total(Decimal("77.25"), "Cash"), Decimal("77.25"))

    def test_card_adds_three_percent(self):
        self.assertEqual(adjusted_total(Decimal("100.00"), "Debit / Tap-to-pay"), Decimal("103.00"))

    def test_merchant_pay_ach_adds_fee(self):
        result = adjusted_total(Decimal("100.00"), "Merchant Pay - ACH")
        expected = Decimal("100.00") + Decimal("100.00") * Decimal("0.0225") + Decimal("0.35")
        self.assertEqual(result, expected)


class TestUnderMinText(TestCase):
    def _text(self, city="Woodland", subtotal="30.00", discount="0"):
        return under_min_text("Hailey", city, Decimal(subtotal), Decimal(discount))

    def test_suggests_adding_when_close_to_minimum(self):
        self.assertIn("Could we add something", self._text())

    def test_cancels_when_far_below_large_minimum(self):
        # Guinda min=250, order=50 -> diff=200 > half of 250 -> cancel
        self.assertIn("cancel", self._text(city="Guinda", subtotal="50.00"))

    def test_dollar_sign_before_order_amount(self):
        self.assertIn("$30.00", self._text())

    def test_dollar_sign_before_city_minimum(self):
        self.assertIn("$35.00", self._text())

    def test_greeting_uses_first_name(self):
        self.assertIn("Hi Hailey", self._text())

    def test_city_name_in_body(self):
        self.assertIn("Woodland", self._text())

    def test_discount_comes_off_the_amount_shown(self):
        self.assertIn("$20.00", self._text(subtotal="30.00", discount="10.00"))


class TestRenderDispatchText(TestCase):  # flagged customers (merchant pay) are read from the database
    def _render(self, name="Hailey", last_name="White", payment_type="Cash"):
        return render_dispatch_text(
            name, last_name, Decimal("13.20"), Decimal("77.25"), payment_type, "2:30", "3:30",
        )

    def test_shows_eta_window(self):
        msg = self._render()
        self.assertIn("between 2:30-3:30", msg)
        self.assertIn("$77.25", msg)
        self.assertNotIn("card fee", msg)

    def test_no_double_space_in_driver_sentence(self):
        self.assertNotIn("  ", self._render())

    def test_debit_card_shows_fee_text_and_adjusted_total(self):
        msg = self._render(payment_type="Debit / Tap-to-pay")
        self.assertIn("including card fee", msg)
        self.assertIn("$79.57", msg)  # 77.25 * 1.03 = 79.5675 -> rounds to 79.57

    def test_merchant_pay_customer_gets_link_notice_and_fee_text(self):
        msg = self._render(name="Imran", last_name="Rahim", payment_type="Merchant Pay - ACH")
        self.assertIn("merchant pay link", msg)
        self.assertIn("including Merchant Pay fee", msg)

    def test_non_merchant_pay_customer_has_no_link_notice(self):
        self.assertNotIn("merchant pay link", self._render())


class TestIsUnderMin(TestCase):  # city minimums are read from the database
    def test_above_minimum(self):
        # Davis min=$25; 65.98 - 13.20 = 52.78
        self.assertFalse(is_under_min(Decimal("65.98"), Decimal("13.20"), "Davis"))

    def test_below_minimum(self):
        # Guinda min=$250
        self.assertTrue(is_under_min(30.00, Decimal("0"), "Guinda"))

    def test_exactly_at_minimum_counts_as_under(self):
        # Davis min=$25; 25.00 is not > 25.00
        self.assertTrue(is_under_min(25.00, Decimal("0"), "Davis"))

    def test_a_city_with_no_minimum_is_never_under(self):
        self.assertFalse(is_under_min(1, Decimal("0"), "Nowhere"))


class TestIsFsaFsm(SimpleTestCase):
    def test_matches_case_insensitively(self):
        self.assertTrue(is_fsa_fsm("Frank Sinatra Adult"))
        self.assertTrue(is_fsa_fsm("frank sinatra medical"))
        self.assertTrue(is_fsa_fsm("  FRANK SINATRA ADULT  "))

    def test_does_not_match_other_names(self):
        self.assertFalse(is_fsa_fsm("Jane Doe"))


class TestGetEffectiveTotal(TestCase):
    def test_no_companion_returns_order_total_unchanged(self):
        order = Order.objects.create(
            order_id="e1", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            total=Decimal("50.00"), delivery_fee=Decimal("5.00"),
        )
        self.assertEqual(effective_total(order), Decimal("50.00"))

    def test_linked_companion_with_matching_fees(self):
        primary = Order.objects.create(
            order_id="e2", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            total=Decimal("50.00"), delivery_fee=Decimal("5.00"),
        )
        Order.objects.create(
            order_id="e3", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            total=Decimal("30.00"), delivery_fee=Decimal("5.00"),
            linked_order=primary,
        )
        self.assertEqual(effective_total(primary), Decimal("75.00"))  # 50 + 30 - 5

    def test_linked_companion_with_mismatched_fees_subtracts_lower(self):
        primary = Order.objects.create(
            order_id="e4", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            total=Decimal("50.00"), delivery_fee=Decimal("7.00"),
        )
        Order.objects.create(
            order_id="e5", customer_name="Frank Sinatra Medical", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            total=Decimal("30.00"), delivery_fee=Decimal("5.00"),
            linked_order=primary,
        )
        self.assertEqual(effective_total(primary), Decimal("75.00"))  # 50 + 30 - min(7,5)


class TestCombinedTotalInMessaging(TestCase):
    def setUp(self):
        self.primary = Order.objects.create(
            order_id="m1", customer_name="Jane Doe", customer_phone="15305551111",
            city="Davis", order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            payment_type="Cash", total=Decimal("50.00"), discount=Decimal("0.00"),
            delivery_fee=Decimal("5.00"), source="POS",
        )
        Order.objects.create(
            order_id="m2", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            total=Decimal("30.00"), delivery_fee=Decimal("5.00"),
            linked_order=self.primary,
        )

    def test_dispatch_msg_uses_combined_total(self):
        from orders.services.customer_texts import dispatch_text as build_dispatch_msg_from_order
        msg = build_dispatch_msg_from_order(self.primary)
        self.assertIn("$75.00", msg)  # 50 + 30 - 5, Cash -> unchanged

    def test_order_notes_use_combined_total(self):
        from orders.services.note import get_order_notes
        notes = get_order_notes(self.primary)
        self.assertIn("77.25", notes)  # 75.00 * 1.03
