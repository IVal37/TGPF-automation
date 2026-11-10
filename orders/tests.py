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
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings

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
from orders import views as orders_views
from orders.models import Driver, Order
from orders.services import queries
from orders.services.engine import add_driver, add_order, complete_order, get_driver_city_queues
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
# the same order as a direct (point-of-sale) sale: carries its own payment label
POS_ORDER = {k: v for k, v in SAMPLE_ORDER.items() if k != "weedmapsOrderId"}
POS_ORDER["source"] = "POS"


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
            # Order is mocked, so the duplicate-webhook check must say "not seen yet"
            "Order": {"objects.filter.return_value.exists.return_value": False},
            "extract_msg_info": {"return_value": {
                "id": "6767008", "name": "Hailey", "last_name": "White",
                "phone": "15307239356", "pay_type": "Cash", "source": "POS",
            }},
            "get_dispatch_msg": {"return_value": "msg"},
            "is_under_min": {"return_value": False},
            "get_order_notes": {"return_value": "notes"},
            "fill_order_notes_task": {},
            "prepare_order_task": {},
            "resolve_payment_type_task": {},
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

    # Weedmaps orders (source "External") get their real payment type from a
    # background task; the webhook itself never opens a browser.

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_weedmaps_order_enqueues_payment_lookup_and_never_scrapes_inline(self):
        with patch("orders.services.scrapers.weedmaps.get_wm_payment_type") as scraper, \
             patch("orders.tasks.get_wm_payment_type") as task_scraper:
            _, mocks = self._run()
        mocks["resolve_payment_type_task"].delay.assert_called_once_with("6767008", write_notes=False)
        scraper.assert_not_called()
        task_scraper.assert_not_called()
        self.assertFalse(hasattr(orders_views, "get_wm_payment_type"))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_weedmaps_order_is_saved_with_empty_payment_type_and_its_weedmaps_id(self):
        _, mocks = self._run()
        self.assertEqual(mocks["add_order"].call_args.kwargs["weedmaps_order_id"], "49018286")
        mocks["Order"].objects.filter.return_value.update.assert_any_call(payment_type="")

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_direct_order_keeps_its_webhook_payment_type_and_skips_lookup(self):
        _, mocks = self._run(payload=POS_ORDER)
        mocks["resolve_payment_type_task"].delay.assert_not_called()
        self.assertEqual(mocks["add_order"].call_args.kwargs["weedmaps_order_id"], "")
        mocks["Order"].objects.filter.return_value.update.assert_any_call(payment_type="Cash")

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_test_mode_enqueues_nothing_and_keeps_webhook_payment_type(self):
        _, mocks = self._run()
        mocks["resolve_payment_type_task"].delay.assert_not_called()
        mocks["prepare_order_task"].delay.assert_not_called()
        mocks["Order"].objects.filter.return_value.update.assert_any_call(payment_type="Cash")

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_weedmaps_no_text_customer_gets_notes_after_payment_resolves(self):
        payload = dict(SAMPLE_ORDER, customer={**SAMPLE_ORDER["customer"], "name": "Mohsen Awara"})
        _, mocks = self._run(payload=payload)
        mocks["fill_order_notes_task"].delay.assert_not_called()
        mocks["resolve_payment_type_task"].delay.assert_called_once_with("6767008", write_notes=True)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_weedmaps_under_min_order_does_not_wait_for_payment_type(self):
        _, mocks = self._run(is_under_min={"return_value": True})
        mocks["fill_order_notes_task"].delay.assert_called_once_with(WJ_ID, "reached out about under min")
        mocks["resolve_payment_type_task"].delay.assert_called_once_with("6767008", write_notes=False)

    # The checks below are pure "does it fire" smoke tests for the
    # Selenium/scraper call sites in new_order: they don't assert anything about
    # what the scraper does, only that the wiring actually reaches it under the
    # right trigger condition, with the real function mocked out so no browser
    # ever launches and no real order/driver data is touched.

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_pending_status_dispatches_to_driver(self):
        payload = dict(SAMPLE_ORDER, status="Pending")
        _, mocks = self._run(payload=payload)
        mocks["prepare_order_task"].delay.assert_called_once_with(WJ_ID, True)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_non_pending_status_does_not_dispatch_to_driver(self):
        _, mocks = self._run()
        mocks["prepare_order_task"].delay.assert_called_once_with(WJ_ID, False)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_normal_path_pulls_from_chiles_but_defers_text_and_notes(self):
        # chiles pull / driver dispatch are unrelated to the ETA text, so they
        # still fire immediately; the SMS + WebJoint notes wait for a dispatcher
        # to approve/edit the ETA window via send_dispatch_msg_view
        _, mocks = self._run()
        mocks["prepare_order_task"].delay.assert_called_once_with(WJ_ID, False)
        mocks["fill_order_notes_task"].delay.assert_not_called()
        mocks["send_message_task"].delay.assert_not_called()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_under_min_order_sends_immediately(self):
        # under-minimum messages have no ETA window to review, so they keep
        # auto-sending exactly as before and never enter the dispatch queue
        _, mocks = self._run(is_under_min={"return_value": True})
        mocks["send_message_task"].delay.assert_called_once_with("15307239356", "msg")
        mocks["fill_order_notes_task"].delay.assert_called_once_with(WJ_ID, "reached out about under min")
        mocks["prepare_order_task"].delay.assert_called_once_with(WJ_ID, False)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_no_text_customer_skips_queue_but_writes_notes(self):
        # the hardcoded excluded customer never gets texted, so there's nothing
        # to hold for approval — notes get written immediately instead
        payload = dict(POS_ORDER, customer={**POS_ORDER["customer"], "name": "Mohsen Awara"})
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
        mocks["prepare_order_task"].delay.assert_not_called()
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
        self.driver = Driver.objects.create(name="D1", is_active=True)
        self.order = Order.objects.create(
            order_id="9001",
            driver=self.driver,
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
        self.order.status = Order.Status.SENT
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
        self.primary.status = Order.Status.SENT
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
            linked_order=primary, dispatch_sent=True, status=Order.Status.SENT,
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
            linked_order=primary, dispatch_sent=True, status=Order.Status.SENT,
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
        msg = _build_restock_message(Order.Status.STOREFRONT)
        self.assertIn("Preroll x3", msg)
        self.assertNotIn("Gummy", msg)

    def test_delivery_message_only_includes_delivery_orders(self):
        from orders.tasks import _build_restock_message
        msg = _build_restock_message(Order.Status.COMPLETED)
        self.assertIn("Gummy x3", msg)
        self.assertNotIn("Preroll", msg)


class TestBuildRestockMessageEmpty(TestCase):
    def test_no_orders_returns_placeholder_text(self):
        from orders.tasks import _build_restock_message
        self.assertEqual(_build_restock_message(Order.Status.STOREFRONT), "No items to restock today.")


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
        # "today" is the fixture day, so the open-order queries and shell dates line up
        patcher = patch("django.utils.timezone.now", return_value=self.BASE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.driver = Driver.objects.create(name="Test Driver", current_city="Davis")
        add_driver(self.driver.pk)

    def _add(self, order_id, city, when=None):
        add_order(order_city=city, order_id=order_id, customer_name="Test", order_time=when or self.BASE)

    def test_single_order_gets_30min_floor_and_60min_window(self):
        # Davis -> Davis is same-city travel (15) + delivery (5) = 20 raw minutes,
        # below the 30-minute floor, so the floor should win.
        self._add("o1", "Davis")
        get_eta("o1")
        order = Order.objects.get(order_id="o1")
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

        o1, o2 = Order.objects.get(order_id="o1"), Order.objects.get(order_id="o2")
        self.assertLessEqual(o1.eta_start, o2.eta_start)
        for o in (o1, o2):
            self.assertEqual(o.eta_end - o.eta_start, timedelta(minutes=60))

    def test_window_widens_past_five_real_orders(self):
        for i in range(6):
            self._add(f"o{i}", "Davis")
        get_eta("o0")
        order = Order.objects.get(order_id="o0")
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=90))

    def test_shell_orders_dont_count_toward_window_widening(self):
        from orders.services.engine import add_shell_order
        self._add("o1", "Davis")
        for _ in range(6):
            add_shell_order("Davis")
        get_eta("o1")
        order = Order.objects.get(order_id="o1")
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_completing_an_order_updates_driver_location_for_next_eta(self):
        self._add("o1", "Fairfield")
        get_eta("o1")
        complete_order("o1")
        live_driver = Driver.objects.get(pk=self.driver.pk)
        self.assertEqual(live_driver.get_current_city(), "Fairfield")

        # next order placed in the city the driver just moved to should only need
        # same-city travel + delivery (15 + 5 = 20 raw minutes), still under the floor.
        self._add("o2", "Fairfield")
        get_eta("o2")
        order2 = Order.objects.get(order_id="o2")
        self.assertEqual(order2.eta_start, self.BASE + timedelta(minutes=30))


# ── duplicate webhooks ──────────────────────────────────────────────────────────

class TestDuplicateWebhook(TestCase):
    """A webhook WebJoint resends (same order id) must be a harmless 200."""

    def _post(self, payload):
        from orders.views import new_order
        request = RequestFactory().post(
            "/new_order", data=json.dumps(payload), content_type="application/json",
        )
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch(f"orders.views.{name}"))
                for name in (
                    "add_order", "add_storefront_order", "get_dispatch_msg", "is_under_min",
                    "prepare_order_task", "resolve_payment_type_task",
                    "fill_order_notes_task", "send_message_task",
                    "send_merchant_pay_link_task",
                )
            }
            stack.enter_context(patch("orders.views.extract_msg_info", return_value={
                "id": "6767008", "name": "Hailey", "last_name": "White",
                "phone": "15307239356", "pay_type": "Cash", "source": "POS",
            }))
            mocks["is_under_min"].return_value = False
            response = new_order(request, key=settings.WEBHOOK_SECRET)
        return response, mocks

    def _existing(self, order_id):
        Order.objects.create(order_id=order_id, customer_name="Hailey White", city="Davis",
                             order_date=datetime(2026, 7, 20, 14, 0, 0))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_duplicate_creates_one_order_and_queues_nothing(self):
        self._existing("6767008")
        with self.assertLogs("orders.views", "INFO") as logs:
            response, mocks = self._post(SAMPLE_ORDER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.filter(order_id="6767008").count(), 1)
        for name, mock in mocks.items():
            self.assertFalse(mock.called or mock.delay.called, f"{name} ran for a duplicate")
        self.assertTrue(any("duplicate webhook" in line for line in logs.output))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_duplicate_third_st_order_is_also_ignored(self):
        payload = dict(
            POS_ORDER, shipping={"number": "526", "street": "Third St", "city": "Davis", "zip": "95616"},
        )
        self._existing("6767008")
        response, mocks = self._post(payload)
        self.assertEqual(response.status_code, 200)
        mocks["add_storefront_order"].assert_not_called()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_a_new_order_id_is_not_treated_as_duplicate(self):
        self._existing("1")
        _, mocks = self._post(SAMPLE_ORDER)
        mocks["add_order"].assert_called_once()


# ── Send is held until the payment type is known ──────────────────────────────

class TestSendDispatchPaymentGate(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.driver = Driver.objects.create(name="D1", is_active=True)
        self.order = Order.objects.create(
            order_id="9001", driver=self.driver, customer_name="Jane Doe", customer_phone="15305551234",
            city="Davis", source="External", payment_type="",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            total=Decimal("50.00"), webjoint_id="1111-2222-3333333-9001",
        )

    def _send(self, **extra):
        from orders.views import send_dispatch_msg_view
        request = self.factory.post("/send_dispatch_msg", {
            "order_id": "9001", "eta_start": "14:45", "eta_end": "15:45", **extra,
        })
        with patch("orders.views.send_message_task") as send_task, \
             patch("orders.views.fill_order_notes_task") as notes_task:
            response = send_dispatch_msg_view(_as_dispatcher(request))
        self.order.refresh_from_db()
        return response, send_task, notes_task

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_send_is_refused_while_payment_type_is_empty(self):
        with self.assertLogs("orders.views", "WARNING"):
            response, send_task, notes_task = self._send()
        self.assertEqual(response.status_code, 302)
        send_task.delay.assert_not_called()
        notes_task.delay.assert_not_called()
        self.assertFalse(self.order.dispatch_sent)
        self.assertEqual(self.order.payment_type, "")
        # the ETA edit isn't applied either: nothing about a refused send sticks
        self.assertEqual(self.order.eta_start, datetime(2026, 7, 20, 14, 30, 0))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_send_as_cash_sets_cash_and_sends(self):
        _, send_task, notes_task = self._send(as_cash="1")
        self.assertEqual(self.order.payment_type, "Cash")
        self.assertTrue(self.order.dispatch_sent)
        send_task.delay.assert_called_once()
        self.assertEqual(notes_task.delay.call_args.args[0], "1111-2222-3333333-9001")
        self.assertIn("WM says cash", notes_task.delay.call_args.args[1])

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_send_as_cash_is_refused_once_the_real_type_has_resolved(self):
        Order.objects.filter(order_id="9001").update(payment_type="Debit / Tap-to-pay")
        with self.assertLogs("orders.views", "WARNING"):
            _, send_task, _ = self._send(as_cash="1")
        send_task.delay.assert_not_called()
        self.assertEqual(self.order.payment_type, "Debit / Tap-to-pay")
        self.assertFalse(self.order.dispatch_sent)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_plain_send_works_once_the_type_has_resolved(self):
        Order.objects.filter(order_id="9001").update(payment_type="Debit / Tap-to-pay")
        _, send_task, _ = self._send()
        send_task.delay.assert_called_once()
        self.assertTrue(self.order.dispatch_sent)


class TestPendingOrdersCard(TestCase):
    def _html(self, payment_type):
        from orders.views import pending_orders_partial
        driver = Driver.objects.create(name="D1", is_active=True)
        Order.objects.create(
            order_id="9001", driver=driver, customer_name="Jane Doe", city="Davis", payment_type=payment_type,
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        request = _as_dispatcher(RequestFactory().get("/pending_orders_partial"))
        return pending_orders_partial(request).content.decode()

    def test_empty_payment_type_shows_checking_disables_send_and_offers_cash(self):
        html = self._html("")
        self.assertIn("Payment: checking", html)
        self.assertIn("disabled", html)
        self.assertIn('name="as_cash"', html)

    def test_resolved_payment_type_is_shown_and_send_is_enabled(self):
        html = self._html("Debit / Tap-to-pay")
        self.assertIn("Payment: Debit / Tap-to-pay", html)
        self.assertNotIn("checking", html)
        self.assertNotIn("disabled", html)
        self.assertNotIn("as_cash", html)


# ── browser tasks ─────────────────────────────────────────────────────────────

class TestPrepareOrderTask(SimpleTestCase):
    def _run(self, dispatch, pull_error=None):
        from orders.tasks import prepare_order_task
        calls = []
        with patch("orders.tasks.pull_from_chiles", side_effect=lambda oid: (calls.append(("pull", oid)), pull_error and (_ for _ in ()).throw(pull_error))), \
             patch("orders.tasks.dispatch_to_driver", side_effect=lambda oid: calls.append(("dispatch", oid))):
            prepare_order_task(WJ_ID, dispatch)
        return calls

    def test_pulls_then_dispatches(self):
        self.assertEqual(self._run(True), [("pull", WJ_ID), ("dispatch", WJ_ID)])

    def test_does_not_dispatch_when_not_asked(self):
        self.assertEqual(self._run(False), [("pull", WJ_ID)])

    def test_still_dispatches_when_the_pull_raises(self):
        with self.assertLogs("orders.tasks", "ERROR") as logs:
            calls = self._run(True, pull_error=RuntimeError("chrome died"))
        self.assertEqual(calls, [("pull", WJ_ID), ("dispatch", WJ_ID)])
        self.assertTrue(any(WJ_ID in line for line in logs.output))


class TestResolvePaymentTypeTask(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            order_id="9001", customer_name="Jane Doe", city="Davis", source="External",
            order_date=datetime(2026, 7, 20, 14, 0, 0), webjoint_id=WJ_ID,
            weedmaps_order_id="49018286", payment_type="", total=Decimal("50.00"),
        )

    def _run(self, result="Debit / Tap-to-pay", **kwargs):
        from orders.tasks import resolve_payment_type_task
        with patch("orders.tasks.get_wm_payment_type", return_value=result) as scraper, \
             patch("orders.tasks.fill_order_notes") as notes:
            resolve_payment_type_task("9001", **kwargs)
        self.order.refresh_from_db()
        return scraper, notes

    def test_looks_up_by_weedmaps_order_id_and_saves_the_result(self):
        scraper, notes = self._run()
        scraper.assert_called_once_with("49018286")
        self.assertEqual(self.order.payment_type, "Debit / Tap-to-pay")
        notes.assert_not_called()

    def test_never_overwrites_a_payment_type_already_set(self):
        # a dispatcher pressed "Send as cash" while the lookup was still running
        Order.objects.filter(order_id="9001").update(payment_type="Cash")
        self._run()
        self.assertEqual(self.order.payment_type, "Cash")

    def test_write_notes_uses_the_resolved_payment_type(self):
        _, notes = self._run(write_notes=True)
        notes.assert_called_once()
        self.assertEqual(notes.call_args.args[0], WJ_ID)
        self.assertIn("TTP", notes.call_args.args[1])

    def test_missing_order_is_a_logged_noop(self):
        from orders.tasks import resolve_payment_type_task
        with patch("orders.tasks.get_wm_payment_type") as scraper, \
             self.assertLogs("orders.tasks", "WARNING"):
            resolve_payment_type_task("nope")
        scraper.assert_not_called()


class TestBrowserQueueSettings(SimpleTestCase):
    def _queue(self, task_name):
        from TGPFflows.celery import app
        return app.amqp.router.route({}, f"orders.tasks.{task_name}")["queue"].name

    def test_every_scraper_backed_task_is_on_the_browser_queue(self):
        for name in ("send_message_task", "fill_order_notes_task", "pull_from_chiles_task",
                     "dispatch_to_driver_task", "prepare_order_task", "resolve_payment_type_task",
                     "create_product_task", "send_merchant_pay_link_task"):
            self.assertEqual(self._queue(name), "browser", name)

    def test_restock_digests_stay_on_the_default_queue(self):
        for name in ("send_third_st_restock", "send_delivery_restock"):
            self.assertEqual(self._queue(name), "celery", name)

    def test_no_task_is_left_unrouted_by_accident(self):
        # a new task in orders/tasks.py must be listed in BROWSER_TASKS or be a restock digest
        from celery.app.task import Task
        from orders import tasks
        names = {n for n, obj in vars(tasks).items() if isinstance(obj, Task)}
        self.assertEqual(names, set(settings.BROWSER_TASKS) | {"send_third_st_restock", "send_delivery_restock"})

    def test_prefetch_is_one_and_acks_are_not_late(self):
        from TGPFflows.celery import app
        self.assertEqual(app.conf.worker_prefetch_multiplier, 1)
        self.assertFalse(app.conf.task_acks_late)


# ── Order.status ──────────────────────────────────────────────────────────────

class TestRestockDigestsUseStatus(TestCase):
    def setUp(self):
        self.today = datetime.now()
        driver = Driver.objects.create(name="D1")
        add_driver(driver.pk)

    def _item(self, order_id, name, qty):
        order = Order.objects.get(order_id=order_id)
        order.items.create(product_name=name, quantity=qty, price=Decimal("1.00"))

    def _order(self, order_id, status, **extra):
        Order.objects.create(order_id=order_id, customer_name="C", city="Davis",
                             order_date=self.today, status=status, **extra)
        self._item(order_id, "Gummy", 1)

    def test_delivery_digest_excludes_cancelled_and_still_open_orders(self):
        from orders.tasks import _build_restock_message
        self._order("done", Order.Status.COMPLETED)
        self._order("cancelled", Order.Status.CANCELLED, completed=True)
        self._order("out", Order.Status.SENT)
        self._order("queued", Order.Status.AWAITING_APPROVAL)
        self.assertEqual(_build_restock_message(Order.Status.COMPLETED), "Gummy x1")

    def test_storefront_digest_counts_only_storefront_orders(self):
        from orders.tasks import _build_restock_message
        self._order("store", Order.Status.STOREFRONT)
        self._order("done", Order.Status.COMPLETED)
        self.assertEqual(_build_restock_message(Order.Status.STOREFRONT), "Gummy x1")

    def test_cancelling_a_tracked_order_keeps_it_out_of_the_delivery_digest(self):
        from orders.services.engine import cancel_order
        from orders.tasks import _build_restock_message
        add_order(
            order_city="Davis", order_id="c-1", customer_name="Cust", order_time=self.today,
            items=[{"quantity": 3, "price": "8.00", "variant": {"product": {"name": "Gummy"}}}],
        )
        cancel_order("c-1")
        self.assertEqual(_build_restock_message(Order.Status.COMPLETED), "No items to restock today.")


class TestEngineWritesStatus(TestCase):
    def setUp(self):
        self.driver = Driver.objects.create(name="D1", current_city="Davis")
        add_driver(self.driver.pk)

    def _add(self, order_id):
        add_order(order_city="Davis", order_id=order_id, customer_name="C", order_time=datetime.now())

    def test_new_order_awaits_approval(self):
        self._add("a1")
        order = Order.objects.get(order_id="a1")
        self.assertEqual(order.status, Order.Status.AWAITING_APPROVAL)
        self.assertFalse(order.is_shell)

    def test_complete_sets_completed(self):
        self._add("a1")
        complete_order("a1")
        order = Order.objects.get(order_id="a1")
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertTrue(order.completed)  # old boolean kept in step
        self.assertIsNone(order.driver)

    def test_cancel_sets_cancelled(self):
        from orders.services.engine import cancel_order
        self._add("a1")
        cancel_order("a1")
        order = Order.objects.get(order_id="a1")
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertTrue(order.completed)  # old boolean kept in step
        self.assertNotIn("a1", [o.order_id for o in queries.open_orders()])

    def test_shell_is_created_sent_and_closed_completed(self):
        from orders.services.engine import add_shell_order
        add_shell_order("Davis")
        shell = Order.objects.get(is_shell=True)
        self.assertTrue(shell.order_id.startswith("shell_"))
        self.assertEqual(shell.status, Order.Status.SENT)
        self._add("a1")
        complete_order("a1")  # completing an order in the shell's city closes the shell
        shell.refresh_from_db()
        self.assertEqual(shell.status, Order.Status.COMPLETED)

    def test_storefront_order_is_storefront(self):
        from orders.services.engine import add_storefront_order
        add_storefront_order(order_id="s1", customer_name="Walk In", order_time=datetime.now())
        order = Order.objects.get(order_id="s1")
        self.assertEqual(order.status, Order.Status.STOREFRONT)
        self.assertTrue(order.is_storefront and order.completed and order.dispatch_sent)


class TestQueueUsesStatus(TestCase):
    def _order(self, order_id, status, **extra):
        fields = {"payment_type": "Cash", **extra}
        driver = Driver.objects.get_or_create(name="D1", defaults={"is_active": True})[0]
        return Order.objects.create(
            driver=driver, order_id=order_id, customer_name=f"Cust {order_id}", customer_phone="15305551234",
            city="Davis", order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            status=status, **fields,
        )

    def test_only_queue_statuses_show_in_the_queue(self):
        from orders.views import _pending_queryset
        self._order("q1", Order.Status.AWAITING_APPROVAL)
        self._order("q2", Order.Status.AWAITING_PAYMENT, payment_type="")
        self._order("s1", Order.Status.SENT)
        self._order("c1", Order.Status.COMPLETED)
        self._order("x1", Order.Status.CANCELLED)
        self._order("sh", Order.Status.AWAITING_APPROVAL, is_shell=True)
        ids = set(_pending_queryset().values_list("order_id", flat=True))
        self.assertEqual(ids, {"q1", "q2"})

    def test_send_ignores_the_old_boolean_and_trusts_status(self):
        from orders.views import send_dispatch_msg_view
        self._order("s1", Order.Status.SENT, dispatch_sent=False)  # booleans disagree
        request = RequestFactory().post("/send_dispatch_msg", {"order_id": "s1"})
        with patch("orders.views.send_message_task") as send_task, self.assertRaises(Http404):
            send_dispatch_msg_view(_as_dispatcher(request))
        send_task.delay.assert_not_called()

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_send_marks_the_order_sent(self):
        from orders.views import send_dispatch_msg_view
        self._order("q1", Order.Status.AWAITING_APPROVAL)
        request = RequestFactory().post("/send_dispatch_msg", {"order_id": "q1"})
        send_dispatch_msg_view(_as_dispatcher(request))
        order = Order.objects.get(order_id="q1")
        self.assertEqual(order.status, Order.Status.SENT)
        self.assertTrue(order.dispatch_sent)

    def test_unlink_puts_companion_back_in_the_queue(self):
        from orders.views import unlink_order_view
        primary = self._order("p1", Order.Status.AWAITING_APPROVAL)
        self._order("f1", Order.Status.SENT, linked_order=primary)
        unlink_order_view(_as_dispatcher(RequestFactory().post("/unlink_order", {"order_id": "f1"})))
        companion = Order.objects.get(order_id="f1")
        self.assertEqual(companion.status, Order.Status.AWAITING_APPROVAL)
        self.assertFalse(companion.dispatch_sent)
        self.assertIsNone(companion.linked_order)

    def test_unlink_keeps_send_held_when_payment_type_is_still_missing(self):
        from orders.views import unlink_order_view
        primary = self._order("p1", Order.Status.AWAITING_APPROVAL)
        self._order("f1", Order.Status.SENT, linked_order=primary, )
        Order.objects.filter(order_id="f1").update(payment_type="")
        unlink_order_view(_as_dispatcher(RequestFactory().post("/unlink_order", {"order_id": "f1"})))
        self.assertEqual(Order.objects.get(order_id="f1").status, Order.Status.AWAITING_PAYMENT)

    def test_resolve_payment_task_moves_order_on_to_awaiting_approval(self):
        from orders.tasks import resolve_payment_type_task
        self._order("w1", Order.Status.AWAITING_PAYMENT, payment_type="", weedmaps_order_id="49018286")
        with patch("orders.tasks.get_wm_payment_type", return_value="Cash"):
            resolve_payment_type_task("w1")
        order = Order.objects.get(order_id="w1")
        self.assertEqual((order.payment_type, order.status), ("Cash", Order.Status.AWAITING_APPROVAL))

    def test_resolve_payment_task_leaves_a_sent_order_alone(self):
        from orders.tasks import resolve_payment_type_task
        self._order("w1", Order.Status.SENT, payment_type="", weedmaps_order_id="49018286")
        with patch("orders.tasks.get_wm_payment_type", return_value="Cash"):
            resolve_payment_type_task("w1")
        self.assertEqual(Order.objects.get(order_id="w1").status, Order.Status.SENT)


class TestNewOrderStatus(SimpleTestCase):
    """The initial status new_order hands to add_order (reuses TestNewOrderView's mocks)."""
    _run = TestNewOrderView._run
    _post = TestNewOrderView._post

    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_weedmaps_order_starts_awaiting_payment(self):
        _, mocks = self._run()
        self.assertEqual(mocks["add_order"].call_args.kwargs["status"], Order.Status.AWAITING_PAYMENT)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_direct_order_starts_awaiting_approval(self):
        _, mocks = self._run(payload=POS_ORDER)
        self.assertEqual(mocks["add_order"].call_args.kwargs["status"], Order.Status.AWAITING_APPROVAL)


# ── 0011 backfill ─────────────────────────────────────────────────────────────

def _status_for():
    import importlib
    return importlib.import_module("orders.migrations.0011_backfill_order_status").status_for


class TestBackfillMapping(SimpleTestCase):
    ETA = datetime(2026, 7, 20, 14, 30)

    def check(self, expected, *, storefront=False, completed=False, sent=False, eta=None, shell=False):
        self.assertEqual(_status_for()(storefront, completed, sent, eta, shell), expected)

    def test_storefront_wins_over_everything(self):
        self.check("storefront", storefront=True, completed=True, sent=True, eta=self.ETA)

    def test_completed_wins_over_sent(self):
        self.check("completed", completed=True, sent=True, eta=self.ETA)

    def test_sent_when_dispatch_sent(self):
        self.check("sent", sent=True, eta=self.ETA)

    def test_awaiting_approval_when_it_has_an_eta(self):
        self.check("awaiting_approval", eta=self.ETA)

    def test_awaiting_payment_when_no_eta(self):
        self.check("awaiting_payment")

    def test_open_shell_is_sent_and_closed_shell_completed(self):
        self.check("sent", shell=True)
        self.check("completed", shell=True, completed=True)


class TestBackfillMigration(TransactionTestCase):
    """Runs the real migrations: seed rows at 0009, migrate to 0011, check each row."""
    migrate_from = [("orders", "0009_order_weedmaps_order_id")]
    migrate_to = [("orders", "0011_backfill_order_status")]

    ROWS = {
        # order_id: (flags, expected status, expected is_shell)
        "store": (dict(is_storefront=True, completed=True, dispatch_sent=True), "storefront", False),
        "done": (dict(completed=True, dispatch_sent=True, eta=True), "completed", False),
        "cancelled-before": (dict(completed=True, eta=True), "completed", False),
        "sent": (dict(dispatch_sent=True, eta=True), "sent", False),
        "queued": (dict(eta=True), "awaiting_approval", False),
        "no-eta": (dict(), "awaiting_payment", False),
        "shell_open": (dict(), "sent", True),
        "shell_closed": (dict(completed=True), "completed", True),
    }

    def tearDown(self):
        # leave the test database fully migrated for whichever test runs next
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_each_old_combination_maps_to_the_right_status(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        Order = executor.loader.project_state(self.migrate_from).apps.get_model("orders", "Order")
        for order_id, (flags, _, _) in self.ROWS.items():
            flags = dict(flags)
            eta = datetime(2026, 7, 20, 14, 30) if flags.pop("eta", False) else None
            Order.objects.create(order_id=order_id, city="Davis", order_date=datetime(2026, 7, 20, 14, 0),
                                 eta_start=eta, **flags)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        Order = executor.loader.project_state(self.migrate_to).apps.get_model("orders", "Order")
        for order_id, (_, status, is_shell) in self.ROWS.items():
            row = Order.objects.get(order_id=order_id)
            self.assertEqual((row.status, row.is_shell), (status, is_shell), order_id)


# ── Phase 5a: the database is the only source of truth ────────────────────────

class TestNoInMemoryState(SimpleTestCase):
    def test_the_state_module_is_gone(self):
        import importlib
        with self.assertRaises(ImportError):
            importlib.import_module("orders.services.state")


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
        add_order(order_city=city, order_id=order_id, customer_name="Test",
                  order_time=when or self.BASE, **kw)


class TestRestartKeepsEverything(DbTruthCase):
    """Simulates a restart: re-runs the startup loader and checks nothing was lost."""

    def _snapshot(self):
        from orders.views import _restock_context, _pending_orders
        return {
            "drivers": {d.pk: d.current_city for d in queries.active_drivers()},
            "open": [(o.order_id, o.driver_id, o.eta_start, o.eta_end) for o in queries.open_orders()],
            "counts": queries.open_order_counts(),
            "real": queries.open_real_order_count(),
            "queues": {d.pk: dict(get_driver_city_queues(d.pk)) for d in queries.active_drivers()},
            "pick": [self._pick(c) for c in ("Davis", "Winters", "Fairfield")],
            "pending": [(i["order"].order_id, i["order"].status) for i in _pending_orders()],
            "restock": _restock_context(),
        }

    def _pick(self, city):
        from orders.services.engine import _pick_driver
        return _pick_driver(city).pk

    def _build(self):
        self.d1 = self._driver("D1")
        self.d2 = self._driver("D2")
        self._add("o1", "Winters")
        self._add("o2", "Davis")
        self._add("o3", "Fairfield")
        from orders.services.engine import add_shell_order
        add_shell_order("Davis")
        for oid in ("o1", "o2", "o3"):
            get_eta(oid)
        Order.objects.filter(order_id="o2").update(status=Order.Status.SENT)
        Order.objects.filter(order_id="o3").update(payment_type="", status=Order.Status.AWAITING_PAYMENT)
        # two finished orders, one already reconciled...
        self._add("done1", "Davis")
        self._add("done2", "Davis")
        for oid, qty in (("done1", 2), ("done2", 3)):
            Order.objects.get(order_id=oid).items.create(product_name="Gummy", quantity=qty, price=Decimal("1"))
            complete_order(oid)
        Order.objects.filter(order_id="done1").update(restock_reconciled=True)
        # ...and a driver who has moved on today
        Driver.objects.get(pk=self.d1.pk).set_current_city("Fairfield")

    def test_restart_changes_nothing_when_drivers_moved_today(self):
        from orders.services.startup import reset_stale_driver_locations
        self._build()
        before = self._snapshot()
        reset_stale_driver_locations()
        after = self._snapshot()
        self.assertEqual(before, after)
        self.assertEqual(Driver.objects.get(pk=self.d1.pk).current_city, "Fairfield")
        # the restock tab and list survive too
        self.assertEqual([o.order_id for o in after["restock"]["completed_orders"]], ["done2"])
        self.assertEqual(after["restock"]["restock_items"], {"Gummy": {"quantity": 2}})

    def test_restart_resets_a_city_set_on_an_earlier_day(self):
        from orders.services.startup import reset_stale_driver_locations
        self._build()
        Driver.objects.filter(pk=self.d1.pk).update(location_updated_at=self.BASE - timedelta(days=1))
        reset_stale_driver_locations()
        driver = Driver.objects.get(pk=self.d1.pk)
        self.assertEqual(driver.current_city, "Davis")
        self.assertEqual(driver.location_updated_at, self.BASE)
        # the other driver, set today, is untouched
        Driver.objects.filter(pk=self.d2.pk).update(current_city="Winters", location_updated_at=self.BASE)
        reset_stale_driver_locations()
        self.assertEqual(Driver.objects.get(pk=self.d2.pk).current_city, "Winters")

    def test_restart_resets_a_city_that_was_never_timestamped(self):
        from orders.services.startup import reset_stale_driver_locations
        driver = Driver.objects.create(name="Old", current_city="Winters", is_active=True)
        reset_stale_driver_locations()
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Davis")

    def test_ready_resets_through_the_apps_hook(self):
        from orders.apps import OrdersConfig
        driver = Driver.objects.create(name="Old", current_city="Winters", is_active=True)
        OrdersConfig("orders", __import__("orders"))._reset_stale_driver_locations()
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Davis")


class TestOpenOrderQueries(DbTruthCase):
    def test_open_orders_are_todays_open_orders_with_a_driver(self):
        self._driver("D1")
        self._add("today", "Davis")
        self._add("yesterday", "Davis", when=self.BASE - timedelta(days=1))
        self._add("closed", "Davis")
        complete_order("closed")
        self._add("cancelled", "Davis")
        from orders.services.engine import cancel_order
        cancel_order("cancelled")
        self.assertEqual([o.order_id for o in queries.open_orders()], ["today"])

    def test_yesterdays_open_order_does_not_count_toward_load_route_or_window(self):
        driver = self._driver("D1")
        for i in range(8):
            self._add(f"old{i}", "Davis", when=self.BASE - timedelta(days=1))
        self._add("o1", "Davis")
        self.assertEqual(queries.open_order_counts(), {driver.pk: 1})
        self.assertEqual(dict(get_driver_city_queues(driver.pk)), {"Davis": ["o1"]})
        get_eta("o1")
        order = Order.objects.get(order_id="o1")
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_open_shells_count_toward_load_but_not_the_window_count(self):
        from orders.services.engine import add_shell_order
        driver = self._driver("D1")
        self._add("o1", "Davis")
        add_shell_order("Davis")
        self.assertEqual(queries.open_order_counts(), {driver.pk: 2})
        self.assertEqual(queries.open_real_order_count(), 1)

    def test_pick_driver_prefers_the_less_loaded_driver(self):
        from orders.services.engine import _pick_driver
        busy, free = self._driver("Busy"), self._driver("Free")
        self._add("o1", "Davis")  # goes to Busy (lowest id on a tie)
        self.assertEqual(Order.objects.get(order_id="o1").driver_id, busy.pk)
        self.assertEqual(_pick_driver("Davis").pk, free.pk)

    def test_route_order_is_the_order_orders_were_added(self):
        driver = self._driver("D1")
        for oid in ("a", "b", "c"):
            self._add(oid, "Davis")
        self.assertEqual(get_driver_city_queues(driver.pk)["Davis"], ["a", "b", "c"])

    def test_a_repeat_complete_webhook_does_not_move_a_driver_or_close_another_shell(self):
        from orders.services.engine import add_shell_order
        driver = self._driver("D1")
        self._add("o1", "Fairfield")
        add_shell_order("Fairfield")
        add_shell_order("Fairfield")
        complete_order("o1", city="Fairfield")  # closes o1 and one shell
        self.assertEqual(Order.objects.filter(is_shell=True, status=Order.Status.COMPLETED).count(), 1)
        Driver.objects.get(pk=driver.pk).set_current_city("Davis")
        complete_order("o1", city="Fairfield")  # same webhook again
        self.assertEqual(Order.objects.filter(is_shell=True, status=Order.Status.COMPLETED).count(), 1)
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Davis")

    def test_completing_an_order_from_before_today_closes_a_matching_shell(self):
        from orders.services.engine import add_shell_order
        driver = self._driver("D1")
        add_shell_order("Fairfield")
        self._add("old", "Fairfield", when=self.BASE - timedelta(days=1))
        complete_order("old", city="Fairfield")
        self.assertEqual(Order.objects.get(is_shell=True).status, Order.Status.COMPLETED)
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Fairfield")


class TestNoStaleSaves(DbTruthCase):
    """A save from an out-of-date instance must not undo fields changed with .update()."""

    def test_set_etas_leaves_other_fields_alone(self):
        self._driver("D1")
        self._add("o1", "Davis")
        stale = Order.objects.get(order_id="o1")
        Order.objects.filter(order_id="o1").update(payment_type="Debit", status=Order.Status.SENT, dispatch_sent=True)
        stale.set_etas(self.BASE, self.BASE + timedelta(hours=1))
        fresh = Order.objects.get(order_id="o1")
        self.assertEqual((fresh.payment_type, fresh.status, fresh.dispatch_sent), ("Debit", Order.Status.SENT, True))
        self.assertEqual(fresh.eta_end, self.BASE + timedelta(hours=1))

    def test_driver_add_order_leaves_other_fields_alone(self):
        driver = self._driver("D1")
        order = Order.objects.create(order_id="x", city="Davis", order_date=self.BASE)
        stale = Order.objects.get(order_id="x")
        Order.objects.filter(order_id="x").update(payment_type="Cash", status=Order.Status.SENT)
        driver.add_order(stale)
        fresh = Order.objects.get(order_id="x")
        self.assertEqual((fresh.payment_type, fresh.status, fresh.driver_id), ("Cash", Order.Status.SENT, driver.pk))

    def test_set_current_city_leaves_other_fields_alone(self):
        driver = self._driver("D1")
        stale = Driver.objects.get(pk=driver.pk)
        Driver.objects.filter(pk=driver.pk).update(is_active=False)
        stale.set_current_city("Winters")
        fresh = Driver.objects.get(pk=driver.pk)
        self.assertFalse(fresh.is_active)
        self.assertEqual((fresh.current_city, fresh.location_updated_at), ("Winters", self.BASE))


class TestRestockTab(DbTruthCase):
    def _completed(self, order_id, items, **kw):
        self._add(order_id, "Davis")
        order = Order.objects.get(order_id=order_id)
        for name, qty in items:
            order.items.create(product_name=name, quantity=qty, price=Decimal("1"))
        complete_order(order_id)
        if kw:
            Order.objects.filter(order_id=order_id).update(**kw)

    def _post(self, view, **data):
        return view(_as_dispatcher(RequestFactory().post("/x", data)))

    def test_full_restock_flow(self):
        from orders.views import _restock_context, reconcile_restock, clear_restock
        self._driver("D1")
        self._completed("a", [("Gummy", 2), ("Preroll", 1)])
        self._completed("b", [("Gummy", 3)])
        ctx = _restock_context()
        self.assertEqual([o.order_id for o in ctx["completed_orders"]], ["a", "b"])
        self.assertEqual(ctx["restock_items"], {})

        self._post(reconcile_restock, order_ids=["a"])
        ctx = _restock_context()
        self.assertEqual([o.order_id for o in ctx["completed_orders"]], ["b"])
        self.assertEqual(ctx["restock_items"], {"Gummy": {"quantity": 2}, "Preroll": {"quantity": 1}})

        self._post(reconcile_restock, order_ids=["b"])
        self.assertEqual(_restock_context()["restock_items"]["Gummy"], {"quantity": 5})

        self._post(clear_restock)
        ctx = _restock_context()
        self.assertEqual((ctx["completed_orders"], ctx["restock_items"]), ([], {}))
        self.assertTrue(Order.objects.get(order_id="a").restock_cleared)

    def test_cleared_orders_are_not_counted_again(self):
        from orders.views import _restock_context, reconcile_restock, clear_restock
        self._driver("D1")
        self._completed("a", [("Gummy", 2)])
        self._post(reconcile_restock, order_ids=["a"])
        self._post(clear_restock)
        self._completed("b", [("Gummy", 1)])
        self._post(reconcile_restock, order_ids=["b"])
        self.assertEqual(_restock_context()["restock_items"], {"Gummy": {"quantity": 1}})

    def test_only_completed_delivery_orders_are_listed(self):
        from orders.services.engine import add_shell_order, add_storefront_order, cancel_order
        from orders.views import _restock_context
        self._driver("D1")
        self._completed("done", [("Gummy", 1)])
        self._add("cancelled", "Davis"); cancel_order("cancelled")
        self._add("open", "Davis")
        add_shell_order("Davis"); complete_order("noop", city="Davis")  # closes the shell
        add_storefront_order(order_id="store", customer_name="Walk In", order_time=self.BASE)
        self.assertEqual([o.order_id for o in _restock_context()["completed_orders"]], ["done"])

    def test_reconcile_ignores_orders_that_are_not_completed(self):
        from orders.views import reconcile_restock
        self._driver("D1")
        self._add("open", "Davis")
        self._post(reconcile_restock, order_ids=["open"])
        self.assertFalse(Order.objects.get(order_id="open").restock_reconciled)

    def test_restock_tab_renders_from_the_database(self):
        from orders.views import restock_partial
        self._driver("D1")
        self._completed("a", [("Gummy", 2)])
        html = restock_partial(_as_dispatcher(RequestFactory().get("/restock_partial"))).content.decode()
        self.assertIn('value="a"', html)
        self.assertIn("No items yet", html)


class TestSetDriverLocationView(DbTruthCase):
    def test_sets_city_and_timestamp(self):
        from orders.views import set_driver_location_view
        driver = self._driver("D1")
        set_driver_location_view(_as_dispatcher(RequestFactory().post("/x", {"driver_id": driver.pk, "city": "Winters"})))
        driver.refresh_from_db()
        self.assertEqual((driver.current_city, driver.location_updated_at), ("Winters", self.BASE))

    def test_inactive_driver_is_a_404_not_a_server_error(self):
        from orders.views import set_driver_location_view
        driver = Driver.objects.create(name="Off", is_active=False)
        with self.assertRaises(Http404):
            set_driver_location_view(_as_dispatcher(RequestFactory().post("/x", {"driver_id": driver.pk, "city": "Winters"})))


class TestBackfillRestockFlags(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_orders_do_not_flood_the_restock_tab(self):
        before, after = [("orders", "0011_backfill_order_status")], [("orders", "0012_restock_flags_driver_location_updated_at")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        Old = executor.loader.project_state(before).apps.get_model("orders", "Order")
        Old.objects.create(order_id="old", city="Davis", order_date=datetime(2026, 7, 1), status="completed", completed=True)
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        New = executor.loader.project_state(after).apps.get_model("orders", "Order")
        row = New.objects.get(order_id="old")
        self.assertEqual((row.restock_reconciled, row.restock_cleared), (True, True))
        new_row = New.objects.create(order_id="new", city="Davis", order_date=datetime(2026, 7, 2))
        self.assertEqual((new_row.restock_reconciled, new_row.restock_cleared), (False, False))


# ── Phase 5b: unassigned orders and driver controls ───────────────────────────

class TestAddOrderWithoutADriver(DbTruthCase):
    def test_order_is_saved_unassigned_instead_of_raising(self):
        with self.assertLogs("orders.services.engine", "WARNING"):
            order = add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        saved = Order.objects.get(order_id="u1")
        self.assertIsNone(saved.driver_id)
        self.assertIsNone(saved.eta_start)
        self.assertEqual(order.pk, saved.pk)
        self.assertEqual([o.order_id for o in queries.unassigned_orders()], ["u1"])

    def test_scheduled_window_is_stored(self):
        start, end = self.BASE + timedelta(hours=3), self.BASE + timedelta(hours=4)
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE,
                  scheduled_start=start, scheduled_end=end)
        saved = Order.objects.get(order_id="u1")
        self.assertEqual((saved.scheduled_start, saved.scheduled_end), (start, end))

    def test_a_shell_still_needs_a_driver(self):
        from orders.services.engine import add_shell_order
        with self.assertRaises(ValueError):
            add_shell_order("Davis")

    def test_shell_view_does_not_crash_without_a_driver(self):
        from orders.views import add_shell_order_view
        with self.assertLogs("orders.views", "WARNING"):
            response = add_shell_order_view(_as_dispatcher(RequestFactory().post("/x", {"city": "Davis"})))
        self.assertEqual(response.status_code, 302)


class TestAssignOrder(DbTruthCase):
    def setUp(self):
        super().setUp()
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        self.driver = self._driver("D1")

    def test_queued_order_gets_a_driver_and_an_eta(self):
        from orders.services.engine import assign_order
        assign_order("u1", self.driver.pk)
        order = Order.objects.get(order_id="u1")
        self.assertEqual(order.driver_id, self.driver.pk)
        self.assertEqual(order.eta_start, self.BASE + timedelta(minutes=30))
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_scheduled_window_wins_when_it_is_no_earlier_than_the_estimate(self):
        from orders.services.engine import assign_order
        start, end = self.BASE + timedelta(hours=3), self.BASE + timedelta(hours=4)
        Order.objects.filter(order_id="u1").update(scheduled_start=start, scheduled_end=end)
        assign_order("u1", self.driver.pk)
        order = Order.objects.get(order_id="u1")
        self.assertEqual((order.eta_start, order.eta_end), (start, end))

    def test_scheduled_window_is_ignored_when_the_estimate_is_later(self):
        from orders.services.engine import assign_order
        start, end = self.BASE, self.BASE + timedelta(minutes=10)
        Order.objects.filter(order_id="u1").update(scheduled_start=start, scheduled_end=end)
        assign_order("u1", self.driver.pk)
        self.assertEqual(Order.objects.get(order_id="u1").eta_start, self.BASE + timedelta(minutes=30))

    def test_an_already_texted_order_keeps_its_eta(self):
        from orders.services.engine import assign_order
        told = (self.BASE + timedelta(hours=1), self.BASE + timedelta(hours=2))
        Order.objects.filter(order_id="u1").update(status=Order.Status.SENT, eta_start=told[0], eta_end=told[1])
        assign_order("u1", self.driver.pk)
        order = Order.objects.get(order_id="u1")
        self.assertEqual((order.driver_id, order.eta_start, order.eta_end), (self.driver.pk, told[0], told[1]))

    def test_inactive_driver_cannot_be_assigned(self):
        from orders.services.engine import assign_order
        Driver.objects.filter(pk=self.driver.pk).update(is_active=False)
        with self.assertRaises(Driver.DoesNotExist):
            assign_order("u1", self.driver.pk)


class TestRemoveAndDeleteDriver(DbTruthCase):
    def setUp(self):
        super().setUp()
        from orders.services.engine import add_shell_order
        self.d1, self.d2 = self._driver("D1"), self._driver("D2")
        self._add("queued", "Davis")                       # -> D1
        self._add("other", "Davis")                        # -> D2
        self._add("out", "Davis"); Order.objects.filter(order_id="out").update(status=Order.Status.SENT, driver=self.d1)
        self._add("done", "Davis"); complete_order("done")
        Order.objects.filter(order_id="queued").update(driver=self.d1)
        add_shell_order("Davis")
        Order.objects.filter(is_shell=True).update(driver=self.d1)

    def test_remove_driver_unassigns_open_orders_including_sent_ones(self):
        from orders.services.engine import remove_driver
        remove_driver(self.d1.pk)
        self.assertFalse(Driver.objects.get(pk=self.d1.pk).is_active)
        for oid in ("queued", "out"):
            self.assertIsNone(Order.objects.get(order_id=oid).driver_id, oid)
        self.assertEqual(Order.objects.get(order_id="out").status, Order.Status.SENT)
        self.assertEqual(Order.objects.get(order_id="queued").status, Order.Status.AWAITING_APPROVAL)
        self.assertEqual(sorted(o.order_id for o in queries.unassigned_orders()), ["out", "queued"])

    def test_remove_driver_closes_their_shells_and_leaves_others_alone(self):
        from orders.services.engine import remove_driver
        remove_driver(self.d1.pk)
        shell = Order.objects.get(is_shell=True)
        self.assertEqual((shell.status, shell.driver_id), (Order.Status.COMPLETED, None))
        self.assertEqual(Order.objects.get(order_id="other").driver_id, self.d2.pk)
        self.assertEqual(Order.objects.get(order_id="done").status, Order.Status.COMPLETED)

    def test_delete_driver_unassigns_then_deletes(self):
        from orders.services.engine import delete_driver
        delete_driver(self.d1.pk)
        self.assertFalse(Driver.objects.filter(pk=self.d1.pk).exists())
        self.assertIsNone(Order.objects.get(order_id="queued").driver_id)
        self.assertEqual(Order.objects.get(order_id="queued").status, Order.Status.AWAITING_APPROVAL)


class TestNewOrderWithNoDriver(DbTruthCase):
    """The real engine and queue, with only the scrapers/SMS mocked: the webhook must not fail."""

    def _post(self, payload):
        from orders.views import new_order
        request = RequestFactory().post("/new_order", data=json.dumps(payload), content_type="application/json")
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch(f"orders.views.{name}")) for name in (
                "prepare_order_task", "resolve_payment_type_task", "fill_order_notes_task",
                "send_message_task", "send_merchant_pay_link_task",
            )}
            mocks["get_dispatch_msg"] = stack.enter_context(
                patch("orders.views.get_dispatch_msg", wraps=orders_views.get_dispatch_msg))
            response = new_order(request, key=settings.WEBHOOK_SECRET)
        return response, mocks

    def _payload(self, **extra):
        return {**POS_ORDER, "id": 7001, "created": self.BASE.isoformat() + "Z", "shipping": {"city": "Davis"},
                "subtotal": 500, "total": 500, "discount": 0, **extra}

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_unassigned_order_is_saved_without_an_eta_and_skips_get_dispatch_msg(self):
        with self.assertLogs("orders.services.engine", "WARNING"):
            response, mocks = self._post(self._payload())
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(order_id="7001")
        self.assertEqual((order.driver_id, order.eta_start, order.status), (None, None, Order.Status.AWAITING_APPROVAL))
        mocks["get_dispatch_msg"].assert_not_called()
        mocks["send_message_task"].delay.assert_not_called()
        # the WebJoint pull/dispatch does not depend on our driver
        mocks["prepare_order_task"].delay.assert_called_once_with(WJ_ID, False)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_under_min_unassigned_order_still_auto_sends(self):
        with self.assertLogs("orders.services.engine", "WARNING"):
            _, mocks = self._post(self._payload(subtotal=10, total=10))
        mocks["send_message_task"].delay.assert_called_once()
        mocks["fill_order_notes_task"].delay.assert_called_once_with(WJ_ID, "reached out about under min")
        order = Order.objects.get(order_id="7001")
        self.assertEqual((order.status, order.driver_id), (Order.Status.SENT, None))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_no_text_customer_is_left_for_assignment(self):
        payload = self._payload(customer={**POS_ORDER["customer"], "name": "Mohsen Awara"})
        with self.assertLogs("orders.services.engine", "WARNING"):
            _, mocks = self._post(payload)
        mocks["fill_order_notes_task"].delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="7001").status, Order.Status.AWAITING_APPROVAL)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_scheduled_window_is_stored_for_later(self):
        cart = {"deliveryTiming": "Scheduled", "scheduledStart": "2026-07-20T22:00:00.000Z",
                "scheduledEnd": "2026-07-20T23:00:00.000Z"}
        with self.assertLogs("orders.services.engine", "WARNING"):
            self._post(self._payload(cart=cart))
        order = Order.objects.get(order_id="7001")
        self.assertIsNotNone(order.scheduled_start)
        self.assertEqual(order.scheduled_end - order.scheduled_start, timedelta(hours=1))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_with_a_driver_on_shift_it_is_a_normal_assigned_order(self):
        self._driver("D1")
        _, mocks = self._post(self._payload())
        order = Order.objects.get(order_id="7001")
        self.assertIsNotNone(order.driver_id)
        self.assertIsNotNone(order.eta_start)
        mocks["get_dispatch_msg"].assert_called_once()


class TestUnassignedQueue(DbTruthCase):
    def _html(self):
        from orders.views import pending_orders_partial
        return pending_orders_partial(_as_dispatcher(RequestFactory().get("/p"))).content.decode()

    def test_unassigned_card_has_a_driver_select_and_no_send(self):
        add_order(order_city="Davis", order_id="u1", customer_name="Pat Q", order_time=self.BASE)
        self._driver("Dee")
        html = self._html()
        self.assertIn("UNASSIGNED", html)
        self.assertIn('action="/assign_driver"', html)
        self.assertIn("Dee", html)
        self.assertNotIn('action="/send_dispatch_msg"', html)
        self.assertIn('name="csrfmiddlewaretoken"', html)

    def test_without_an_active_driver_the_card_says_to_switch_one_on(self):
        add_order(order_city="Davis", order_id="u1", customer_name="Pat Q", order_time=self.BASE)
        html = self._html()
        self.assertIn("Switch a driver on", html)
        self.assertNotIn('action="/assign_driver"', html)

    def test_already_texted_orders_are_marked(self):
        add_order(order_city="Davis", order_id="u1", customer_name="Pat Q", order_time=self.BASE)
        Order.objects.filter(order_id="u1").update(status=Order.Status.SENT)
        self.assertIn("Already texted", self._html())

    def test_unassigned_cards_come_first_and_an_assigned_card_is_normal(self):
        self._driver("Dee")
        self._add("assigned", "Davis"); get_eta("assigned")
        Order.objects.filter(order_id="assigned").update(customer_name="Assigned Cust")
        Order.objects.create(order_id="loose", customer_name="Loose Cust", city="Davis", order_date=self.BASE)
        html = self._html()
        self.assertLess(html.index("Loose Cust"), html.index("Assigned Cust"))
        self.assertEqual(html.count('action="/send_dispatch_msg"'), 1)

    def test_yesterdays_unassigned_order_is_not_shown(self):
        add_order(order_city="Davis", order_id="old", customer_name="Old Cust",
                  order_time=self.BASE - timedelta(days=1))
        self.assertNotIn("Old Cust", self._html())

    def test_send_is_refused_for_an_unassigned_order(self):
        from orders.views import send_dispatch_msg_view
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        Order.objects.filter(order_id="u1").update(payment_type="Cash")
        with patch("orders.views.send_message_task") as send_task, self.assertLogs("orders.views", "WARNING"):
            send_dispatch_msg_view(_as_dispatcher(RequestFactory().post("/x", {"order_id": "u1"})))
        send_task.delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="u1").status, Order.Status.AWAITING_APPROVAL)


class TestAssignDriverView(DbTruthCase):
    def _assign(self, order_id, driver_id):
        from orders.views import assign_driver_view
        return assign_driver_view(_as_dispatcher(
            RequestFactory().post("/assign_driver", {"order_id": order_id, "driver_id": driver_id})))

    def setUp(self):
        super().setUp()
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        self.driver = self._driver("D1")

    def test_card_becomes_a_normal_queue_card(self):
        from orders.views import _pending_orders
        before = _pending_orders()
        self.assertTrue(before[0]["unassigned"])
        self._assign("u1", self.driver.pk)
        after = _pending_orders()
        self.assertFalse(after[0]["unassigned"])
        self.assertEqual(after[0]["eta_start_value"], "14:30")

    def test_already_assigned_order_is_a_404(self):
        self._assign("u1", self.driver.pk)
        with self.assertRaises(Http404):
            self._assign("u1", self.driver.pk)

    def test_inactive_driver_is_a_404(self):
        Driver.objects.filter(pk=self.driver.pk).update(is_active=False)
        with self.assertRaises(Http404):
            self._assign("u1", self.driver.pk)
        self.assertIsNone(Order.objects.get(order_id="u1").driver_id)

    def test_sent_order_is_assigned_without_texting_anyone(self):
        Order.objects.filter(order_id="u1").update(status=Order.Status.SENT)
        with patch("orders.views.send_message_task") as send_task, patch("orders.views.fill_order_notes_task") as notes:
            self._assign("u1", self.driver.pk)
        send_task.delay.assert_not_called()
        notes.delay.assert_not_called()
        order = Order.objects.get(order_id="u1")
        self.assertEqual((order.driver_id, order.status), (self.driver.pk, Order.Status.SENT))
        self.assertIsNone(order.eta_start)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_no_text_customer_gets_notes_and_is_marked_sent_on_assignment(self):
        Order.objects.filter(order_id="u1").update(
            customer_name="Mohsen Awara", payment_type="Cash", webjoint_id=WJ_ID)
        with patch("orders.views.fill_order_notes_task") as notes, patch("orders.views.send_message_task") as send_task:
            self._assign("u1", self.driver.pk)
        notes.delay.assert_called_once()
        self.assertEqual(notes.delay.call_args.args[0], WJ_ID)
        self.assertIn("ETA:", notes.delay.call_args.args[1])
        send_task.delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="u1").status, Order.Status.SENT)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_no_text_customer_still_awaiting_payment_defers_the_note_to_the_lookup(self):
        Order.objects.filter(order_id="u1").update(
            customer_name="Mohsen Awara", payment_type="", status=Order.Status.AWAITING_PAYMENT)
        with patch("orders.views.fill_order_notes_task") as notes, \
             patch("orders.views.resolve_payment_type_task") as resolve:
            self._assign("u1", self.driver.pk)
        notes.delay.assert_not_called()
        resolve.delay.assert_called_once_with("u1", write_notes=True)

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_test_mode_sends_and_writes_nothing(self):
        Order.objects.filter(order_id="u1").update(customer_name="Mohsen Awara", payment_type="Cash")
        with patch("orders.views.fill_order_notes_task") as notes:
            self._assign("u1", self.driver.pk)
        notes.delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="u1").status, Order.Status.AWAITING_APPROVAL)


class TestDriverControlViews(DbTruthCase):
    def _post(self, view, **data):
        return view(_as_dispatcher(RequestFactory().post("/x", data)))

    def test_add_driver_creates_an_on_shift_driver_starting_in_davis(self):
        from orders.views import add_driver_view
        self._post(add_driver_view, name="  Sam Rivera ")
        driver = Driver.objects.get(name="Sam Rivera")
        self.assertEqual((driver.is_active, driver.current_city), (True, "Davis"))

    def test_add_driver_ignores_a_blank_name(self):
        from orders.views import add_driver_view
        self._post(add_driver_view, name="   ")
        self.assertEqual(Driver.objects.count(), 0)

    def test_the_old_dummy_driver_view_is_gone(self):
        self.assertFalse(hasattr(orders_views, "add_dummy_driver"))

    def test_toggle_switches_a_driver_off_and_unassigns_their_orders(self):
        from orders.views import toggle_driver_view
        driver = self._driver("D1")
        self._add("o1", "Davis")
        self._post(toggle_driver_view, driver_id=driver.pk)
        self.assertFalse(Driver.objects.get(pk=driver.pk).is_active)
        self.assertIsNone(Order.objects.get(order_id="o1").driver_id)

    def test_toggle_switches_a_driver_back_on(self):
        from orders.views import toggle_driver_view
        driver = Driver.objects.create(name="D1", is_active=False)
        self._post(toggle_driver_view, driver_id=driver.pk)
        self.assertTrue(Driver.objects.get(pk=driver.pk).is_active)

    def test_toggle_unknown_driver_is_a_404(self):
        from orders.views import toggle_driver_view
        with self.assertRaises(Http404):
            self._post(toggle_driver_view, driver_id=999)

    def test_delete_driver_unassigns_their_orders(self):
        from orders.views import delete_driver_view
        driver = self._driver("D1")
        self._add("o1", "Davis")
        self._post(delete_driver_view, driver_id=driver.pk)
        self.assertFalse(Driver.objects.filter(pk=driver.pk).exists())
        self.assertIsNone(Order.objects.get(order_id="o1").driver_id)


class TestStartupPanel(DbTruthCase):
    def _page(self):
        from django.test import Client
        client = Client()
        client.force_login(User.objects.get_or_create(username="dispatcher")[0])
        return client.get("/").content.decode()

    def test_panel_has_add_driver_toggle_delete_with_confirm(self):
        driver = self._driver("Dee")
        html = self._page()
        self.assertIn('action="/add_driver"', html)
        self.assertIn('action="/toggle_driver"', html)
        self.assertIn('action="/delete_driver"', html)
        self.assertIn("confirm(", html)
        self.assertIn("Switch off", html)
        self.assertNotIn("add_dummy_driver", html)
        Driver.objects.filter(pk=driver.pk).update(is_active=False)
        self.assertIn("Switch on", self._page())

    def test_out_for_delivery_lists_each_drivers_sent_orders_with_mark_delivered(self):
        d1, d2 = self._driver("Dee"), self._driver("Eve")
        self._add("sent1", "Davis"); self._add("queued1", "Davis")
        Order.objects.filter(order_id="sent1").update(
            status=Order.Status.SENT, driver=d1, customer_name="Out Cust")
        Order.objects.filter(order_id="queued1").update(driver=d2, customer_name="Queued Cust")
        html = self._page()
        self.assertIn("Out Cust", html)
        self.assertEqual(html.count('action="/complete_order_manual"'), 1)
        self.assertIn('name="order_id" value="sent1"', html)
        self.assertNotIn("Queued Cust</span>", html)
        self.assertIn("Nothing out for delivery", html)  # Eve has none

    def test_mark_delivered_completes_the_order(self):
        from orders.views import complete_order_manual
        driver = self._driver("Dee")
        self._add("sent1", "Davis")
        Order.objects.filter(order_id="sent1").update(status=Order.Status.SENT)
        complete_order_manual(_as_dispatcher(RequestFactory().post("/x", {"order_id": "sent1"})))
        self.assertEqual(Order.objects.get(order_id="sent1").status, Order.Status.COMPLETED)
        self.assertNotIn('name="order_id" value="sent1"', self._page())  # gone from out-for-delivery


class TestPhase5bSecurity(TestCase):
    NEW_VIEWS = ("/add_driver", "/toggle_driver", "/assign_driver")

    def test_anonymous_posts_redirect_to_login(self):
        from django.test import Client
        for path in self.NEW_VIEWS:
            with self.subTest(path=path):
                response = Client().post(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_posts_without_a_csrf_token_are_forbidden(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_user("dispatcher"))
        for path in self.NEW_VIEWS:
            with self.subTest(path=path):
                self.assertEqual(client.post(path).status_code, 403)

    def test_get_is_not_allowed(self):
        from django.test import Client
        client = Client()
        client.force_login(User.objects.create_user("dispatcher"))
        for path in self.NEW_VIEWS:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 405)
