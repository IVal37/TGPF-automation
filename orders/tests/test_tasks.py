from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from orders.models import Driver, Order
from orders.services.engine import add_driver, add_order, complete_order
from orders.tests.helpers import WJ_ID


class TestBuildRestockMessage(TestCase):
    def setUp(self):

        from orders.services.engine import add_storefront_order

        today = datetime.now()

        # two storefront sales today, same product — should sum
        add_storefront_order(
            order_id="t-store-1",
            customer_name="Walk In",
            order_time=today,
            items=[{"quantity": 2, "price": "5.00", "variant": {"product": {"name": "Preroll"}}}],
        )
        add_storefront_order(
            order_id="t-store-2",
            customer_name="Walk In 2",
            order_time=today,
            items=[{"quantity": 1, "price": "5.00", "variant": {"product": {"name": "Preroll"}}}],
        )

        # a completed delivery order today
        driver = Driver.objects.create(name="D1")
        add_driver(driver.pk)
        add_order(
            order_city="Davis",
            order_id="t-delivery-1",
            customer_name="Cust",
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
    @override_settings(TELEGRAM_BOT_TOKEN="abc123", TELEGRAM_CHAT_ID="-100999")
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

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    @patch("orders.services.telegram.requests.post")
    def test_missing_config_does_not_attempt_send(self, mock_post):
        from orders.services.telegram import send_telegram_message

        send_telegram_message("hello")
        mock_post.assert_not_called()


class TestPrepareOrderTask(SimpleTestCase):
    def _run(self, dispatch, pull_error=None):
        from orders.tasks import prepare_order_task

        calls = []
        with patch(
            "orders.tasks.pull_from_chiles",
            side_effect=lambda oid: (calls.append(("pull", oid)), pull_error and (_ for _ in ()).throw(pull_error)),
        ), patch("orders.tasks.dispatch_to_driver", side_effect=lambda oid: calls.append(("dispatch", oid))):
            prepare_order_task(WJ_ID, dispatch)
        return calls

    def test_pulls_then_dispatches(self):
        self.assertEqual(self._run(True), [("pull", WJ_ID), ("dispatch", WJ_ID)])

    def test_does_not_dispatch_when_not_asked(self):
        self.assertEqual(self._run(False), [("pull", WJ_ID)])

    def test_still_dispatches_when_the_pull_raises(self):
        # a pull that failed after its commit is raised (so it shows on the card and is
        # never retried), but only once the dispatch has run
        with self.assertLogs("orders.tasks", "ERROR") as logs, self.assertRaises(RuntimeError):
            self._run(True, pull_error=RuntimeError("chrome died"))
        self.assertTrue(any(WJ_ID in line for line in logs.output))

    def test_dispatch_runs_even_when_the_pull_fails(self):
        from orders.tasks import prepare_order_task

        calls = []
        with patch("orders.tasks.pull_from_chiles", side_effect=RuntimeError("chrome died")), patch(
            "orders.tasks.dispatch_to_driver", side_effect=lambda oid: calls.append(oid)
        ), self.assertLogs("orders.tasks", "ERROR"), self.assertRaises(RuntimeError):
            prepare_order_task(WJ_ID, True)
        self.assertEqual(calls, [WJ_ID])


class TestResolvePaymentTypeTask(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            order_id="9001",
            customer_name="Jane Doe",
            city="Davis",
            source="External",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            webjoint_id=WJ_ID,
            weedmaps_order_id="49018286",
            payment_type="",
            total=Decimal("50.00"),
        )

    def _run(self, result="Debit / Tap-to-pay", **kwargs):
        from orders.tasks import resolve_payment_type_task

        with patch("orders.tasks.get_wm_payment_type", return_value=result) as scraper, patch(
            "orders.tasks.fill_order_notes"
        ) as notes:
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

        with patch("orders.tasks.get_wm_payment_type") as scraper, self.assertLogs("orders.tasks", "WARNING"):
            resolve_payment_type_task("nope")
        scraper.assert_not_called()


class TestBrowserQueueSettings(SimpleTestCase):
    def _queue(self, task_name):
        from TGPFflows.celery import app

        return app.amqp.router.route({}, f"orders.tasks.{task_name}")["queue"].name

    def test_every_scraper_backed_task_is_on_the_browser_queue(self):
        for name in (
            "send_message_task",
            "fill_order_notes_task",
            "pull_from_chiles_task",
            "dispatch_to_driver_task",
            "prepare_order_task",
            "resolve_payment_type_task",
            "create_product_task",
        ):
            self.assertEqual(self._queue(name), "browser", name)

    def test_restock_digests_and_the_merchant_pay_reminder_stay_on_the_default_queue(self):
        # the merchant pay reminder opens no browser (it only posts a dashboard notice)
        for name in ("send_third_st_restock", "send_delivery_restock", "send_merchant_pay_link_task"):
            self.assertEqual(self._queue(name), "celery", name)

    def test_no_task_is_left_unrouted_by_accident(self):
        # a new task in orders/tasks.py must be listed in BROWSER_TASKS or be a restock digest
        from celery.app.task import Task

        from orders import tasks

        names = {n for n, obj in vars(tasks).items() if isinstance(obj, Task)}
        self.assertEqual(
            names,
            set(settings.BROWSER_TASKS)
            | {
                "send_third_st_restock",
                "send_delivery_restock",
                "send_merchant_pay_link_task",
            },
        )

    def test_prefetch_is_one_and_acks_are_not_late(self):
        from TGPFflows.celery import app

        self.assertEqual(app.conf.worker_prefetch_multiplier, 1)
        self.assertFalse(app.conf.task_acks_late)


class TestRestockDigestsUseStatus(TestCase):
    def setUp(self):
        self.today = datetime.now()
        driver = Driver.objects.create(name="D1")
        add_driver(driver.pk)

    def _item(self, order_id, name, qty):
        order = Order.objects.get(order_id=order_id)
        order.items.create(product_name=name, quantity=qty, price=Decimal("1.00"))

    def _order(self, order_id, status, **extra):
        Order.objects.create(
            order_id=order_id, customer_name="C", city="Davis", order_date=self.today, status=status, **extra
        )
        self._item(order_id, "Gummy", 1)

    def test_delivery_digest_excludes_cancelled_and_still_open_orders(self):
        from orders.tasks import _build_restock_message

        self._order("done", Order.Status.COMPLETED)
        self._order("cancelled", Order.Status.CANCELLED)
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
            order_city="Davis",
            order_id="c-1",
            customer_name="Cust",
            order_time=self.today,
            items=[{"quantity": 3, "price": "8.00", "variant": {"product": {"name": "Gummy"}}}],
        )
        cancel_order("c-1")
        self.assertEqual(_build_restock_message(Order.Status.COMPLETED), "No items to restock today.")
