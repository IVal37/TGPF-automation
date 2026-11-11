import json
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings

from orders.models import Order
from orders.tests.helpers import POS_ORDER, SAMPLE_ORDER, WJ_ID, DbTruthCase


class TestNewOrderView(
    TestCase
):  # config (city minimums, flagged customers, special addresses) is read from the database
    def setUp(self):
        self.factory = RequestFactory()

    def _post(self, payload=None):
        return self.factory.post(
            "/new_order",
            data=json.dumps(payload or SAMPLE_ORDER),
            content_type="application/json",
        )

    # the browser tasks are patched where they are defined; everything else where intake uses it
    TASK_NAMES = (
        "fill_order_notes_task",
        "prepare_order_task",
        "resolve_payment_type_task",
        "send_message_task",
        "send_merchant_pay_link_task",
    )

    def _run(self, payload=None, **overrides):
        """Runs the new_order view with every external side effect (DB writes, scrapers, texting)
        mocked out, so tests can exercise the intake orchestration in isolation.
        Pass e.g. is_under_min={"return_value": True} to override a default.
        Returns (response, {patch_target_name: mock})."""
        defaults = {
            "add_order": {},
            "add_storefront_order": {},
            # Order is mocked, so the duplicate-webhook check must say "not seen yet"
            "Order": {"objects.filter.return_value.exists.return_value": False},
            "estimate_window": {},
            "under_min_text": {"return_value": "msg"},
            "is_under_min": {"return_value": False},
            "get_order_notes": {"return_value": "notes"},
            **{name: {} for name in self.TASK_NAMES},
        }
        defaults.update(overrides)

        from orders.views import new_order

        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(
                    patch(
                        f"orders.tasks.{name}" if name in self.TASK_NAMES else f"orders.services.intake.{name}",
                        **kwargs,
                    )
                )
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
        mocks["estimate_window"].assert_called_once()
        mocks["under_min_text"].assert_not_called()
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
        payload = dict(
            SAMPLE_ORDER,
            shipping={
                "city": "Davis",
                "number": "526",
                "street": "3rd Street",
                "zip": "95616",
            },
        )
        _, mocks = self._run(payload=payload)
        mocks["add_storefront_order"].assert_called_once()
        mocks["add_order"].assert_not_called()
        mocks["estimate_window"].assert_not_called()

    def test_non_third_st_davis_order_still_dispatches_normally(self):
        # sanity check the routing isn't overbroad — an ordinary Davis delivery
        # address must still go through the normal dispatch flow
        payload = dict(
            SAMPLE_ORDER,
            shipping={
                "city": "Davis",
                "number": "100",
                "street": "Main Street",
                "zip": "95616",
            },
        )
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
        with patch("orders.services.scrapers.weedmaps.get_wm_payment_type") as scraper, patch(
            "orders.tasks.get_wm_payment_type"
        ) as task_scraper:
            _, mocks = self._run()
        mocks["resolve_payment_type_task"].delay.assert_called_once_with("6767008", write_notes=False)
        scraper.assert_not_called()
        task_scraper.assert_not_called()
        from orders import views

        self.assertFalse(hasattr(views, "get_wm_payment_type"))

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
        mocks["send_message_task"].delay.assert_called_once_with("6767008", "15307239356", "msg")
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
        with self.assertLogs("orders.services.intake", "WARNING") as logs:
            _, mocks = self._run(payload=payload, is_under_min={"return_value": True})
        self.assertEqual(mocks["add_order"].call_args.kwargs["webjoint_id"], "")
        mocks["send_message_task"].delay.assert_called_once_with("6767008", "15307239356", "msg")
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
        payload = dict(POS_ORDER, customer={**POS_ORDER["customer"], "name": "Imran Rahim"})
        _, mocks = self._run(payload=payload)
        mocks["send_merchant_pay_link_task"].delay.assert_called_once_with("6767008")

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_merchant_pay_customer_under_min_does_not_send_link(self):
        # the merchant-pay-link SMS line only appears in the normal dispatch text,
        # never the under-min one, so the link itself shouldn't send either
        payload = dict(POS_ORDER, customer={**POS_ORDER["customer"], "name": "Imran Rahim"})
        _, mocks = self._run(payload=payload, is_under_min={"return_value": True})
        mocks["send_merchant_pay_link_task"].delay.assert_not_called()


class TestDuplicateWebhook(TestCase):
    """A webhook WebJoint resends (same order id) must be a harmless 200."""

    def _post(self, payload):
        from orders.views import new_order

        request = RequestFactory().post(
            "/new_order",
            data=json.dumps(payload),
            content_type="application/json",
        )
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch(f"orders.services.intake.{name}"))
                for name in ("add_order", "add_storefront_order", "estimate_window", "under_min_text", "is_under_min")
            }
            mocks.update(
                {
                    name: stack.enter_context(patch(f"orders.tasks.{name}"))
                    for name in (
                        "prepare_order_task",
                        "resolve_payment_type_task",
                        "fill_order_notes_task",
                        "send_message_task",
                        "send_merchant_pay_link_task",
                    )
                }
            )
            mocks["is_under_min"].return_value = False
            response = new_order(request, key=settings.WEBHOOK_SECRET)
        return response, mocks

    def _existing(self, order_id):
        Order.objects.create(
            order_id=order_id, customer_name="Hailey White", city="Davis", order_date=datetime(2026, 7, 20, 14, 0, 0)
        )

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_duplicate_creates_one_order_and_queues_nothing(self):
        self._existing("6767008")
        with self.assertLogs("orders.services.intake", "INFO") as logs:
            response, mocks = self._post(SAMPLE_ORDER)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.filter(order_id="6767008").count(), 1)
        for name, mock in mocks.items():
            self.assertFalse(mock.called or mock.delay.called, f"{name} ran for a duplicate")
        self.assertTrue(any("duplicate webhook" in line for line in logs.output))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_duplicate_third_st_order_is_also_ignored(self):
        payload = dict(
            POS_ORDER,
            shipping={"number": "526", "street": "Third St", "city": "Davis", "zip": "95616"},
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


class TestNewOrderStatus(
    TestCase
):  # config (city minimums, flagged customers, special addresses) is read from the database
    """The initial status new_order hands to add_order (reuses TestNewOrderView's mocks)."""

    TASK_NAMES = TestNewOrderView.TASK_NAMES
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


class TestNewOrderWithNoDriver(DbTruthCase):
    """The real engine and queue, with only the scrapers/SMS mocked: the webhook must not fail."""

    def _post(self, payload):
        from orders.views import new_order

        request = RequestFactory().post("/new_order", data=json.dumps(payload), content_type="application/json")
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch(f"orders.tasks.{name}"))
                for name in (
                    "prepare_order_task",
                    "resolve_payment_type_task",
                    "fill_order_notes_task",
                    "send_message_task",
                    "send_merchant_pay_link_task",
                )
            }
            from orders.services import intake

            mocks["estimate_window"] = stack.enter_context(
                patch("orders.services.intake.estimate_window", wraps=intake.estimate_window)
            )
            response = new_order(request, key=settings.WEBHOOK_SECRET)
        return response, mocks

    def _payload(self, **extra):
        return {
            **POS_ORDER,
            "id": 7001,
            "created": self.BASE.isoformat() + "Z",
            "shipping": {"city": "Davis"},
            "subtotal": 500,
            "total": 500,
            "discount": 0,
            **extra,
        }

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_unassigned_order_is_saved_without_an_eta_and_skips_the_eta_estimate(self):
        with self.assertLogs("orders.services.engine", "WARNING"):
            response, mocks = self._post(self._payload())
        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(order_id="7001")
        self.assertEqual((order.driver_id, order.eta_start, order.status), (None, None, Order.Status.AWAITING_APPROVAL))
        mocks["estimate_window"].assert_not_called()
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
        # the text is queued; send_message_task moves it on to sent once it is out
        self.assertEqual((order.status, order.driver_id), (Order.Status.SENDING, None))

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_no_text_customer_is_left_for_assignment(self):
        payload = self._payload(customer={**POS_ORDER["customer"], "name": "Mohsen Awara"})
        with self.assertLogs("orders.services.engine", "WARNING"):
            _, mocks = self._post(payload)
        mocks["fill_order_notes_task"].delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="7001").status, Order.Status.AWAITING_APPROVAL)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_scheduled_window_is_stored_for_later(self):
        cart = {
            "deliveryTiming": "Scheduled",
            "scheduledStart": "2026-07-20T22:00:00.000Z",
            "scheduledEnd": "2026-07-20T23:00:00.000Z",
        }
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
        mocks["estimate_window"].assert_called_once()


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
        self.assertEqual(order.status, Order.Status.STOREFRONT)
        self.assertIsNone(order.driver_id)
        self.assertIsNone(order.eta_start)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.first().product_name, "Widget")
        self.assertEqual(order.items.first().quantity, 2)
