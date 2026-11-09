import json
import os
from contextlib import ExitStack
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, ANY

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from orders.services.dispatch import (
    extract_msg_info,
    get_dispatch_msg,
    normal_dispatch_msg,
    under_min_msg,
    get_adjusted_total,
    get_effective_total,
    is_fsa_fsm,
    CalculateForMinCheck,
    populate_items,
)
from orders.models import Driver, Order
from orders.services import state
from orders.services.engine import add_driver, add_order, complete_order
from orders.services.eta import get_eta

def _as_dispatcher(request):
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
    def _eta(self):
        return patch("orders.services.dispatch.get_eta", return_value=("2:30", "3:30"))

    def test_shows_eta_window(self):
        with self._eta():
            msg = normal_dispatch_msg(_msg())
        self.assertIn("between 2:30-3:30", msg)
        self.assertIn("$77.25", msg)
        self.assertNotIn("card fee", msg)

    def test_no_double_space_in_driver_sentence(self):
        with self._eta():
            msg = normal_dispatch_msg(_msg())
        self.assertNotIn("  ", msg)

    def test_debit_card_shows_fee_text_and_adjusted_total(self):
        with self._eta():
            msg = normal_dispatch_msg(_msg(pay_type="Debit / Tap-to-pay"))
        self.assertIn("including card fee", msg)
        self.assertIn("$79.57", msg)  # 77.25 * 1.03 = 79.5675 → rounds to 79.57

    def test_merchant_pay_customer_gets_link_notice_and_fee_text(self):
        with self._eta():
            msg = normal_dispatch_msg(_msg(
                name="Imran", last_name="Rahim", pay_type="Merchant Pay - ACH"
            ))
        self.assertIn("merchant pay link", msg)
        self.assertIn("including Merchant Pay fee", msg)

    def test_non_merchant_pay_customer_has_no_link_notice(self):
        with self._eta():
            msg = normal_dispatch_msg(_msg())
        self.assertNotIn("merchant pay link", msg)

# ── get_dispatch_msg routing ──────────────────────────────────────────────────

class TestGetDispatchMsgRouting(SimpleTestCase):
    def _eta(self):
        return patch("orders.services.dispatch.get_eta", return_value=("2:30", "3:30"))

    def test_above_minimum_produces_normal_message(self):
        # Davis min=$25; 65.98 - 13.20 = 52.78 → above minimum
        with self._eta():
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

    def _run(self, payload=None, **overrides):
        """Runs new_order with every external side effect (DB writes, scrapers, texting)
        mocked out, so tests can exercise the view's own orchestration in isolation.
        Pass e.g. extract_msg_info={"return_value": {...}} to override a default.
        Returns (response, {patch_target_name: mock})."""
        defaults = {
            "add_order": {},
            "add_storefront_order": {},
            "Order": {},
            "extract_msg_info": {"return_value": {
                "id": "6767008", "name": "Hailey", "last_name": "White",
                "phone": "15307239356", "pay_type": "Cash", "source": "POS",
            }},
            "get_dispatch_msg": {"return_value": "msg"},
            "get_wm_payment_type": {"return_value": "Cash"},
            "is_under_min": {"return_value": False},
            "get_order_notes": {"return_value": "notes"},
            "fill_order_notes_task": {},
            "pull_from_chiles_task": {},
            "dispatch_to_driver_task": {},
            "send_message_task": {},
            "send_merchant_pay_link_task": {},
        }
        defaults.update(overrides)

        from orders.views import new_order
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch(f"orders.views.{name}", **kwargs))
                for name, kwargs in defaults.items()
            }
            response = new_order(self._post(payload), key=settings.WEBHOOK_SECRET)
        return response, mocks

    def test_returns_200(self):
        response, _ = self._run()
        self.assertEqual(response.status_code, 200)

    def test_full_chain_is_called(self):
        response, mocks = self._run()
        mocks["add_order"].assert_called_once()
        mocks["extract_msg_info"].assert_called_once()
        mocks["get_dispatch_msg"].assert_called_once()
        # normal (non-under-min) orders no longer text immediately — they sit in
        # the dispatch queue until a dispatcher approves/edits the ETA and sends
        mocks["send_message_task"].delay.assert_not_called()

    def test_add_order_receives_correct_fields(self):
        _, mocks = self._run()
        kw = mocks["add_order"].call_args.kwargs
        self.assertEqual(kw["order_id"], "6767008")
        self.assertEqual(kw["order_city"], "Davis")
        self.assertEqual(kw["customer_name"], "Hailey White")
        self.assertEqual(kw["customer_phone"], "15307239356")
        self.assertEqual(kw["source"], "External")
        self.assertEqual(kw["discount"], Decimal("13.2"))
        self.assertEqual(kw["subtotal"], Decimal("65.98"))
        self.assertEqual(kw["total"], Decimal("77.25"))

    def test_third_st_order_routes_to_storefront_not_dispatch(self):
        # 526 3rd St Davis is the storefront's own address — no driver, no ETA,
        # no dispatch SMS, just persisted for the nightly restock digest
        payload = dict(SAMPLE_ORDER, shipping={
            "city": "Davis", "number": "526", "street": "3rd Street", "zip": "95616",
        })
        _, mocks = self._run(payload=payload)
        mocks["add_storefront_order"].assert_called_once()
        mocks["add_order"].assert_not_called()
        mocks["extract_msg_info"].assert_not_called()

    def test_non_third_st_davis_order_still_dispatches_normally(self):
        # sanity check the routing isn't overbroad — an ordinary Davis delivery
        # address must still go through the normal dispatch flow
        payload = dict(SAMPLE_ORDER, shipping={
            "city": "Davis", "number": "100", "street": "Main Street", "zip": "95616",
        })
        _, mocks = self._run(payload=payload)
        mocks["add_order"].assert_called_once()
        mocks["add_storefront_order"].assert_not_called()

    def test_order_time_parsed_from_created_field(self):
        """'created_at' does not exist in the payload; key is 'created' with Z suffix.
        The view converts the UTC webhook timestamp to naive local wall-clock time
        (to match the naive local `datetime.now()` used everywhere else, e.g. hours.py's
        business-hours checks), so the expectation is computed the same way here rather
        than comparing against the raw UTC value."""
        response, mocks = self._run()
        self.assertEqual(response.status_code, 200)
        expected = datetime.fromisoformat("2025-12-07T19:26:23.124+00:00").astimezone().replace(tzinfo=None)
        self.assertEqual(mocks["add_order"].call_args.kwargs["order_time"], expected)

    def test_items_come_from_details_not_items_count(self):
        """data['items'] is 2 (int count); the actual list is under data['details']."""
        _, mocks = self._run()
        items = mocks["add_order"].call_args.kwargs["items"]
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 2)

    def test_external_source_scrapes_payment_type(self):
        _, mocks = self._run(extract_msg_info={"return_value": {
            "id": "6767008", "name": "Hailey", "last_name": "White",
            "phone": "15307239356", "pay_type": "Cash", "source": "External",
        }})
        mocks["get_wm_payment_type"].assert_called_once_with(49018286)

    def test_pos_source_does_not_scrape(self):
        _, mocks = self._run()
        mocks["get_wm_payment_type"].assert_not_called()

    # The four checks below are pure "does it fire" smoke tests for the
    # Selenium/scraper call sites in new_order: they don't assert anything about
    # what the scraper does, only that the wiring actually reaches it under the
    # right trigger condition, with the real function mocked out so no browser
    # ever launches and no real order/driver data is touched.

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_pending_status_dispatches_to_driver(self):
        payload = dict(SAMPLE_ORDER, status="Pending")
        _, mocks = self._run(payload=payload)
        mocks["dispatch_to_driver_task"].delay.assert_called_once_with(WJ_ID)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_non_pending_status_does_not_dispatch_to_driver(self):
        _, mocks = self._run()
        mocks["dispatch_to_driver_task"].delay.assert_not_called()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_normal_path_pulls_from_chiles_but_defers_text_and_notes(self):
        # chiles pull / driver dispatch are unrelated to the ETA text, so they
        # still fire immediately; the SMS + WebJoint notes wait for a dispatcher
        # to approve/edit the ETA window via send_dispatch_msg_view
        _, mocks = self._run()
        mocks["pull_from_chiles_task"].delay.assert_called_once_with(WJ_ID)
        mocks["fill_order_notes_task"].delay.assert_not_called()
        mocks["send_message_task"].delay.assert_not_called()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_under_min_order_sends_immediately(self):
        # under-minimum messages have no ETA window to review, so they keep
        # auto-sending exactly as before and never enter the dispatch queue
        _, mocks = self._run(is_under_min={"return_value": True})
        mocks["send_message_task"].delay.assert_called_once_with("15307239356", "msg")
        mocks["fill_order_notes_task"].delay.assert_called_once_with(WJ_ID, "reached out about under min")
        mocks["pull_from_chiles_task"].delay.assert_called_once_with(WJ_ID)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_no_text_customer_skips_queue_but_writes_notes(self):
        # the hardcoded excluded customer never gets texted, so there's nothing
        # to hold for approval — notes get written immediately instead
        payload = dict(SAMPLE_ORDER, customer={**SAMPLE_ORDER["customer"], "name": "Mohsen Awara"})
        _, mocks = self._run(payload=payload)
        mocks["send_message_task"].delay.assert_not_called()
        mocks["fill_order_notes_task"].delay.assert_called_once_with(WJ_ID, "notes")

    def test_order_is_saved_with_its_webjoint_id(self):
        _, mocks = self._run()
        self.assertEqual(mocks["add_order"].call_args.kwargs["webjoint_id"], WJ_ID)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_webhook_without_webjoint_id_skips_webjoint_tasks_but_still_texts(self):
        payload = {k: v for k, v in SAMPLE_ORDER.items() if k != "_id"}
        with self.assertLogs("orders.views", "WARNING") as logs:
            _, mocks = self._run(payload=payload, is_under_min={"return_value": True})
        self.assertEqual(mocks["add_order"].call_args.kwargs["webjoint_id"], "")
        mocks["send_message_task"].delay.assert_called_once_with("15307239356", "msg")
        mocks["fill_order_notes_task"].delay.assert_not_called()
        mocks["pull_from_chiles_task"].delay.assert_not_called()
        self.assertTrue(any("6767008" in line for line in logs.output))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_non_merchant_pay_customer_does_not_send_link(self):
        _, mocks = self._run()
        mocks["send_merchant_pay_link_task"].delay.assert_not_called()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_merchant_pay_customer_sends_link(self):
        # "Imran Rahim" is one of the three hardcoded MERCHANT_PAY_CUSTOMERS
        _, mocks = self._run(extract_msg_info={"return_value": {
            "id": "6767008", "name": "Imran", "last_name": "Rahim",
            "phone": "15307239356", "pay_type": "Cash", "source": "POS",
        }})
        mocks["send_merchant_pay_link_task"].delay.assert_called_once()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_merchant_pay_customer_under_min_does_not_send_link(self):
        # the merchant-pay-link SMS line only appears in normal_dispatch_msg,
        # never under_min_msg, so the link itself shouldn't send either
        _, mocks = self._run(
            extract_msg_info={"return_value": {
                "id": "6767008", "name": "Imran", "last_name": "Rahim",
                "phone": "15307239356", "pay_type": "Cash", "source": "POS",
            }},
            is_under_min={"return_value": True},
        )
        mocks["send_merchant_pay_link_task"].delay.assert_not_called()


# ── send_dispatch_msg / send_reschedule_msg views ───────────────────────────────
# These use a real DB row (TestCase, not SimpleTestCase) since the views read
# and write Order fields directly rather than going through mocked helpers —
# only the Selenium-touching send_message/fill_order_notes calls are mocked.

class TestSendDispatchMsgView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.order = Order.objects.create(
            order_id="9001",
            customer_name="Jane Doe",
            customer_phone="15305551234",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
            payment_type="Cash",
            total=Decimal("50.00"),
            discount=Decimal("0.00"),
            webjoint_id="1111-2222-3333333-9001",
        )
        # a newer order that arrived since: notes must still go to order 9001
        Order.objects.create(
            order_id="9099", customer_name="Newer Customer", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 20, 0),
            eta_start=datetime(2026, 7, 20, 15, 0, 0), eta_end=datetime(2026, 7, 20, 16, 0, 0),
            webjoint_id="1111-2222-4444444-9099",
        )

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_sends_message_with_edited_window_and_marks_dispatch_sent(self):
        from orders.views import send_dispatch_msg_view
        with patch("orders.views.send_message_task") as mock_send_task, \
             patch("orders.views.fill_order_notes_task") as mock_notes_task:
            request = self.factory.post("/send_dispatch_msg", {
                "order_id": "9001", "eta_start": "14:45", "eta_end": "15:45",
            })
            response = send_dispatch_msg_view(_as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        mock_send_task.delay.assert_called_once()
        self.assertEqual(mock_send_task.delay.call_args.args[0], "15305551234")
        self.assertIn("2:45-3:45", mock_send_task.delay.call_args.args[1])
        mock_notes_task.delay.assert_called_once()
        # the note targets this order's WebJoint id, not the newer order's
        self.assertEqual(mock_notes_task.delay.call_args.args[0], "1111-2222-3333333-9001")

        self.order.refresh_from_db()
        self.assertTrue(self.order.dispatch_sent)
        self.assertEqual(self.order.eta_start, datetime(2026, 7, 20, 14, 45, 0))
        self.assertEqual(self.order.eta_end, datetime(2026, 7, 20, 15, 45, 0))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_order_without_webjoint_id_is_texted_but_note_is_skipped(self):
        from orders.views import send_dispatch_msg_view
        Order.objects.filter(order_id="9001").update(webjoint_id="")
        with patch("orders.views.send_message_task") as mock_send_task, \
             patch("orders.views.fill_order_notes_task") as mock_notes_task, \
             self.assertLogs("orders.views", "WARNING") as logs:
            request = self.factory.post("/send_dispatch_msg", {
                "order_id": "9001", "eta_start": "14:45", "eta_end": "15:45",
            })
            send_dispatch_msg_view(_as_dispatcher(request))

        mock_send_task.delay.assert_called_once()
        mock_notes_task.delay.assert_not_called()
        self.assertTrue(any("9001" in line for line in logs.output))

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_test_mode_marks_sent_without_texting(self):
        from orders.views import send_dispatch_msg_view
        with patch("orders.views.send_message_task") as mock_send_task:
            request = self.factory.post("/send_dispatch_msg", {
                "order_id": "9001", "eta_start": "14:30", "eta_end": "15:30",
            })
            send_dispatch_msg_view(_as_dispatcher(request))

        mock_send_task.delay.assert_not_called()
        self.order.refresh_from_db()
        self.assertTrue(self.order.dispatch_sent)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_already_sent_order_is_not_resent(self):
        # guards against double-clicking Send and texting the customer twice
        from orders.views import send_dispatch_msg_view
        self.order.dispatch_sent = True
        self.order.save()
        request = self.factory.post("/send_dispatch_msg", {
            "order_id": "9001", "eta_start": "14:30", "eta_end": "15:30",
        })
        with self.assertRaises(Http404):
            send_dispatch_msg_view(_as_dispatcher(request))


class TestSendRescheduleMsgView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        # Monday 10:30pm — past the 10pm weekday closing time
        self.order = Order.objects.create(
            order_id="9002",
            customer_name="John Smith",
            customer_phone="15305559999",
            city="Davis",
            order_date=datetime(2026, 7, 20, 21, 0, 0),
            eta_start=datetime(2026, 7, 20, 22, 30, 0),
            eta_end=datetime(2026, 7, 20, 23, 30, 0),
            webjoint_id="1111-2222-3333333-9002",
        )
        Order.objects.create(
            order_id="9098", customer_name="Newer Customer", city="Davis",
            order_date=datetime(2026, 7, 20, 22, 0, 0),
            eta_start=datetime(2026, 7, 20, 23, 0, 0), eta_end=datetime(2026, 7, 21, 0, 0, 0),
            webjoint_id="1111-2222-4444444-9098",
        )

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_sends_reschedule_and_marks_dispatch_sent(self):
        from orders.views import send_reschedule_msg_view
        with patch("orders.views.send_message_task") as mock_send_task, \
             patch("orders.views.fill_order_notes_task") as mock_notes_task:
            request = self.factory.post("/send_reschedule_msg", {"order_id": "9002"})
            response = send_reschedule_msg_view(_as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        mock_send_task.delay.assert_called_once_with("15305559999", ANY)
        self.assertIn("reschedule", mock_send_task.delay.call_args.args[1].lower())
        mock_notes_task.delay.assert_called_once_with(
            "1111-2222-3333333-9002", "reached out about rescheduling for tomorrow morning",
        )

        self.order.refresh_from_db()
        self.assertTrue(self.order.dispatch_sent)


# ── FSA/FSM legal-limit split linking ───────────────────────────────────────────

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
        self.assertEqual(get_effective_total(order), Decimal("50.00"))

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
        self.assertEqual(get_effective_total(primary), Decimal("75.00"))  # 50 + 30 - 5

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
        self.assertEqual(get_effective_total(primary), Decimal("75.00"))  # 50 + 30 - min(7,5)


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
        from orders.services.dispatch import build_dispatch_msg_from_order
        msg = build_dispatch_msg_from_order(self.primary)
        self.assertIn("$75.00", msg)  # 50 + 30 - 5, Cash -> unchanged

    def test_order_notes_use_combined_total(self):
        from orders.services.note import get_order_notes
        notes = get_order_notes(self.primary)
        self.assertIn("77.25", notes)  # 75.00 * 1.03


class TestLinkOrderView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.primary = Order.objects.create(
            order_id="l1", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            total=Decimal("50.00"), delivery_fee=Decimal("5.00"),
            webjoint_id="1111-2222-3333333-1001",
        )
        self.companion = Order.objects.create(
            order_id="l2", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            total=Decimal("30.00"), delivery_fee=Decimal("5.00"),
            webjoint_id="1111-2222-5555555-1002",
        )

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_links_companion_and_writes_note(self):
        from orders.views import link_order_view
        with patch("orders.views.fill_order_notes_task") as mock_notes_task:
            request = self.factory.post("/link_order", {
                "order_id": "l2", "primary_order_id": "l1",
            })
            response = link_order_view(_as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        self.companion.refresh_from_db()
        self.assertEqual(self.companion.linked_order_id, self.primary.pk)
        self.assertTrue(self.companion.dispatch_sent)
        # the note is written on the companion's WebJoint order, not the primary's
        mock_notes_task.delay.assert_called_once_with(
            "1111-2222-5555555-1002", "second part of Jane Doe's order",
        )

    def test_rejects_non_fsa_fsm_order(self):
        other = Order.objects.create(
            order_id="l3", customer_name="Some Rando", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        from orders.views import link_order_view
        request = self.factory.post("/link_order", {
            "order_id": "l3", "primary_order_id": "l1",
        })
        link_order_view(_as_dispatcher(request))
        other.refresh_from_db()
        self.assertIsNone(other.linked_order)
        self.assertFalse(other.dispatch_sent)

    def test_rejects_when_primary_already_sent(self):
        self.primary.dispatch_sent = True
        self.primary.save()
        from orders.views import link_order_view
        request = self.factory.post("/link_order", {
            "order_id": "l2", "primary_order_id": "l1",
        })
        with self.assertRaises(Http404):
            link_order_view(_as_dispatcher(request))


class TestUnlinkOrderView(TestCase):
    def test_reverses_link(self):
        primary = Order.objects.create(
            order_id="u1", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
        )
        companion = Order.objects.create(
            order_id="u2", customer_name="Frank Sinatra Medical", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            linked_order=primary, dispatch_sent=True,
        )
        from orders.views import unlink_order_view
        factory = RequestFactory()
        request = factory.post("/unlink_order", {"order_id": "u2"})
        response = unlink_order_view(_as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        companion.refresh_from_db()
        self.assertIsNone(companion.linked_order)
        self.assertFalse(companion.dispatch_sent)


class TestClearDispatchQueueView(TestCase):
    def test_marks_all_pending_orders_dispatch_sent_without_texting(self):
        Order.objects.create(
            order_id="c1", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        Order.objects.create(
            order_id="c2", customer_name="John Smith", city="Woodland",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        # under-min order (no eta) was never in the queue — should be untouched
        Order.objects.create(
            order_id="c3", customer_name="Under Min", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
        )

        from orders.views import clear_dispatch_queue_view
        factory = RequestFactory()
        with patch("orders.views.send_message_task") as mock_send_task, \
             patch("orders.views.fill_order_notes_task") as mock_notes_task:
            request = factory.post("/clear_dispatch_queue")
            response = clear_dispatch_queue_view(_as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        mock_send_task.delay.assert_not_called()
        mock_notes_task.delay.assert_not_called()

        self.assertTrue(Order.objects.get(order_id="c1").dispatch_sent)
        self.assertTrue(Order.objects.get(order_id="c2").dispatch_sent)
        self.assertFalse(Order.objects.get(order_id="c3").dispatch_sent)


class TestPendingOrdersFsaFsm(TestCase):
    def test_linked_companion_disappears_from_queue_and_primary_shows_it(self):
        Order.objects.create(
            order_id="p1", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        primary = Order.objects.get(order_id="p1")
        Order.objects.create(
            order_id="p2", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            linked_order=primary, dispatch_sent=True,
        )
        from orders.views import _pending_orders
        result = _pending_orders()
        order_ids = [item["order"].order_id for item in result]
        self.assertIn("p1", order_ids)
        self.assertNotIn("p2", order_ids)  # absorbed — dispatch_sent=True filters it out

        primary_item = next(item for item in result if item["order"].order_id == "p1")
        self.assertEqual(primary_item["linked_companion"].order_id, "p2")

    def test_fsa_fsm_card_shows_link_targets_excluding_itself_and_other_fsa(self):
        Order.objects.create(
            order_id="p3", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        Order.objects.create(
            order_id="p4", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        Order.objects.create(
            order_id="p5", customer_name="Frank Sinatra Medical", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        from orders.views import _pending_orders
        result = _pending_orders()
        fsa_item = next(item for item in result if item["order"].order_id == "p4")
        self.assertTrue(fsa_item["is_fsa_fsm"])
        target_ids = [t.order_id for t in fsa_item["link_targets"]]
        self.assertEqual(target_ids, ["p3"])  # excludes itself and the other FSA/FSM order


# ── add_storefront_order (3rd St tracking) ──────────────────────────────────────

class TestAddStorefrontOrder(TestCase):
    def test_creates_completed_storefront_order_with_items(self):
        from orders.services.engine import add_storefront_order
        add_storefront_order(
            order_id="s1",
            customer_name="Walk In",
            order_time=datetime(2026, 7, 20, 15, 0, 0),
            source="POS",
            discount=Decimal("0"),
            subtotal=Decimal("20.00"),
            total=Decimal("20.00"),
            items=[{"quantity": 2, "price": "10.00", "variant": {"product": {"name": "Widget"}}}],
        )
        order = Order.objects.get(order_id="s1")
        self.assertTrue(order.is_storefront)
        self.assertTrue(order.completed)
        self.assertTrue(order.dispatch_sent)
        self.assertIsNone(order.driver_id)
        self.assertIsNone(order.eta_start)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.first().product_name, "Widget")
        self.assertEqual(order.items.first().quantity, 2)


# ── Telegram restock digest tasks ───────────────────────────────────────────────

class TestBuildRestockMessage(TestCase):
    def setUp(self):
        state.orders_by_id.clear()
        state.driver_id_by_order_id.clear()
        state.drivers_by_id.clear()
        state.active_drivers.clear()

        from orders.services.engine import add_storefront_order
        today = datetime.now()

        # two storefront sales today, same product — should sum
        add_storefront_order(
            order_id="t-store-1", customer_name="Walk In", order_time=today,
            items=[{"quantity": 2, "price": "5.00", "variant": {"product": {"name": "Preroll"}}}],
        )
        add_storefront_order(
            order_id="t-store-2", customer_name="Walk In 2", order_time=today,
            items=[{"quantity": 1, "price": "5.00", "variant": {"product": {"name": "Preroll"}}}],
        )

        # a completed delivery order today
        driver = Driver.objects.create(name="D1")
        add_driver(driver.pk)
        add_order(
            order_city="Davis", order_id="t-delivery-1", customer_name="Cust",
            order_time=today,
            items=[{"quantity": 3, "price": "8.00", "variant": {"product": {"name": "Gummy"}}}],
        )
        complete_order(order_id="t-delivery-1", city="Davis")

    def test_storefront_message_sums_quantities_and_excludes_delivery(self):
        from orders.tasks import _build_restock_message
        msg = _build_restock_message(is_storefront=True)
        self.assertIn("Preroll x3", msg)
        self.assertNotIn("Gummy", msg)

    def test_delivery_message_only_includes_delivery_orders(self):
        from orders.tasks import _build_restock_message
        msg = _build_restock_message(is_storefront=False)
        self.assertIn("Gummy x3", msg)
        self.assertNotIn("Preroll", msg)


class TestBuildRestockMessageEmpty(TestCase):
    def test_no_orders_returns_placeholder_text(self):
        from orders.tasks import _build_restock_message
        self.assertEqual(_build_restock_message(is_storefront=True), "No items to restock today.")


class TestSendTelegramMessage(SimpleTestCase):
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "abc123", "TELEGRAM_CHAT_ID": "-100999"})
    @patch("orders.services.telegram.requests.post")
    def test_posts_to_bot_api_with_chat_id_and_text(self, mock_post):
        from orders.services.telegram import send_telegram_message
        mock_post.return_value.raise_for_status = lambda: None
        send_telegram_message("hello")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn("abc123", args[0])
        self.assertEqual(kwargs["data"]["chat_id"], "-100999")
        self.assertEqual(kwargs["data"]["text"], "hello")

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
    @patch("orders.services.telegram.requests.post")
    def test_missing_config_does_not_attempt_send(self, mock_post):
        from orders.services.telegram import send_telegram_message
        send_telegram_message("hello")
        mock_post.assert_not_called()


# ── add_products_from_invoice view ──────────────────────────────────────────────

class TestAddProductsFromInvoiceView(SimpleTestCase):
    def test_create_product_is_called(self):
        # Selenium call-site smoke test: confirms the view actually enqueues
        # create_product_task with the scraped products, without launching a
        # real browser or touching real webjoint inventory.
        from orders.views import add_products_from_invoice
        factory = RequestFactory()
        invoice_file = SimpleUploadedFile("invoice.pdf", b"fake pdf bytes", content_type="application/pdf")
        with patch("orders.views.extract_products_from_invoice", return_value=[{"name": "X"}]) as mock_extract, \
             patch("orders.views.create_product_task") as mock_create_task:
            request = factory.post("/add_products_from_invoice", {"invoice": invoice_file})
            response = add_products_from_invoice(_as_dispatcher(request))
        mock_extract.assert_called_once()
        mock_create_task.delay.assert_called_once_with([{"name": "X"}])
        self.assertEqual(response.status_code, 302)


# ── get_eta ───────────────────────────────────────────────────────────────────

class TestGetEta(TestCase):
    # Monday 2pm: well clear of the 10:30am opening clamp and the 10pm/5pm closing times,
    # so none of the business-hours logic interferes with the math below.
    BASE = datetime(2026, 7, 20, 14, 0, 0)

    def setUp(self):
        state.orders_by_id.clear()
        state.driver_id_by_order_id.clear()
        state.drivers_by_id.clear()
        state.active_drivers.clear()
        self.driver = Driver.objects.create(name="Test Driver", current_city="Davis")
        add_driver(self.driver.pk)

    def _add(self, order_id, city, when=None):
        add_order(order_city=city, order_id=order_id, customer_name="Test", order_time=when or self.BASE)

    def test_single_order_gets_30min_floor_and_60min_window(self):
        # Davis -> Davis is same-city travel (15) + delivery (5) = 20 raw minutes,
        # below the 30-minute floor, so the floor should win.
        self._add("o1", "Davis")
        get_eta("o1")
        order = state.orders_by_id["o1"]
        self.assertEqual(order.eta_start, self.BASE + timedelta(minutes=30))
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_etas_never_go_backwards_down_the_route(self):
        # Regression test for a bug where the sliding anchor (which pulls a later
        # stop's window start earlier, toward the window's middle) was sized off
        # the window length instead of the real travel time between stops. With
        # two orders in the same city, the old formula pulled the second order's
        # window a full 30 minutes earlier regardless of the real ~20-minute gap
        # between the stops, inverting the two windows (confirmed against the
        # pre-fix code: o2 landed at 14:30, 15 min *before* o1's 14:45).
        #
        # Driver starts in Davis. o1 is the first stop, in Winters
        # (Davis->Winters = 30+5 = 35 raw min). o2 is the second stop, same
        # city as o1 (+15+5 = 20 more, cumulative 55).
        self._add("o1", "Winters")
        self._add("o2", "Winters")

        get_eta("o1")
        get_eta("o2")

        o1, o2 = state.orders_by_id["o1"], state.orders_by_id["o2"]
        self.assertLessEqual(o1.eta_start, o2.eta_start)
        for o in (o1, o2):
            self.assertEqual(o.eta_end - o.eta_start, timedelta(minutes=60))

    def test_window_widens_past_five_real_orders(self):
        for i in range(6):
            self._add(f"o{i}", "Davis")
        get_eta("o0")
        order = state.orders_by_id["o0"]
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=90))

    def test_shell_orders_dont_count_toward_window_widening(self):
        from orders.services.engine import add_shell_order
        self._add("o1", "Davis")
        for _ in range(6):
            add_shell_order("Davis")
        get_eta("o1")
        order = state.orders_by_id["o1"]
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_completing_an_order_updates_driver_location_for_next_eta(self):
        self._add("o1", "Fairfield")
        get_eta("o1")
        complete_order("o1")
        live_driver = state.drivers_by_id[self.driver.pk]
        self.assertEqual(live_driver.get_current_city(), "Fairfield")

        # next order placed in the city the driver just moved to should only need
        # same-city travel + delivery (15 + 5 = 20 raw minutes), still under the floor.
        self._add("o2", "Fairfield")
        get_eta("o2")
        order2 = state.orders_by_id["o2"]
        self.assertEqual(order2.eta_start, self.BASE + timedelta(minutes=30))
