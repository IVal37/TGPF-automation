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

    def test_delivery_message_is_the_ticked_off_delivery_orders_not_storefront_sales(self):
        from orders.services import restock

        restock.reconcile(["t-delivery-1"])
        msg = restock.restock_message()
        self.assertEqual(msg, "Gummy x3")
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
    def _run(self, dispatch, failures=()):
        from orders.tasks import prepare_order_task

        with patch("orders.tasks.prepare_order", return_value=list(failures)) as scraper:
            prepare_order_task(WJ_ID, dispatch)
        return scraper

    def test_runs_the_one_session_scraper_for_the_order(self):
        self._run(True).assert_called_once_with(WJ_ID, True)

    def test_does_not_dispatch_when_not_asked(self):
        self._run(False).assert_called_once_with(WJ_ID, False)

    def test_a_step_that_failed_after_its_commit_is_raised(self):
        from orders.services.scrapers.webjoint import StepFailure

        # raised (so it shows on the card and is never retried), but only after prepare_order
        # has run both steps in its single session
        with self.assertRaises(RuntimeError):
            self._run(True, [StepFailure("pull_from_chiles", RuntimeError("chrome died"))])


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
        from orders.services import restock

        self._order("done", Order.Status.COMPLETED)
        self._order("cancelled", Order.Status.CANCELLED)
        self._order("out", Order.Status.SENT)
        self._order("queued", Order.Status.AWAITING_APPROVAL)
        restock.reconcile(["done", "cancelled", "out", "queued"])  # only a completed order can be ticked off
        self.assertEqual(restock.restock_message(), "Gummy x1")

    def test_delivery_digest_is_exactly_the_dashboard_restock_list(self):
        from orders.services import restock

        self._order("a", Order.Status.COMPLETED)
        self._order("b", Order.Status.COMPLETED)
        self._item("b", "Preroll", 4)
        restock.reconcile(["a", "b"])
        shown = restock.restock_context()["restock_items"]
        self.assertEqual(
            restock.restock_message().split("\n"),
            [f"{name} x{info['quantity']}" for name, info in shown.items()],
        )
        self.assertEqual(shown, {"Gummy": {"quantity": 2}, "Preroll": {"quantity": 4}})

    def test_delivery_digest_follows_complete_restock(self):
        from orders.services import restock

        self._order("done", Order.Status.COMPLETED)
        restock.reconcile(["done"])
        restock.clear_restock_list()
        self.assertEqual(restock.restock_message(), "No items to restock.")

    def test_delivery_digest_says_when_completed_orders_are_not_ticked_off_yet(self):
        from orders.services import restock

        self._order("done", Order.Status.COMPLETED)
        self._order("done2", Order.Status.COMPLETED)
        self.assertEqual(
            restock.restock_message(),
            "No items to restock.\n(2 completed orders not ticked off under Restock Time yet)",
        )
        restock.reconcile(["done"])
        self.assertEqual(
            restock.restock_message(),
            "Gummy x1\n(1 completed order not ticked off under Restock Time yet)",
        )

    def test_storefront_digest_counts_only_storefront_orders(self):
        from orders.tasks import _build_restock_message

        self._order("store", Order.Status.STOREFRONT)
        self._order("done", Order.Status.COMPLETED)
        self.assertEqual(_build_restock_message(Order.Status.STOREFRONT), "Gummy x1")

    def test_cancelling_a_tracked_order_keeps_it_out_of_the_delivery_digest(self):
        from orders.services import restock
        from orders.services.engine import cancel_order

        add_order(
            order_city="Davis",
            order_id="c-1",
            customer_name="Cust",
            order_time=self.today,
            items=[{"quantity": 3, "price": "8.00", "variant": {"product": {"name": "Gummy"}}}],
        )
        cancel_order("c-1")
        self.assertEqual(restock.restock_message(), "No items to restock.")


class TestStaleDigests(SimpleTestCase):
    """A digest that runs long after its scheduled time (Celery beat catching up when the stack
    starts, or a job a downed worker finally picks up) is skipped instead of posted stale."""

    def _at(self, hour, minute=0, day=13):
        # naive local time, as the project runs with USE_TZ off
        return datetime(2026, 11, day, hour, minute, 5)

    def test_on_time_and_slightly_late_are_not_stale(self):
        from orders.tasks import is_stale_digest

        self.assertFalse(is_stale_digest(20, 30, self._at(20, 30)))
        self.assertFalse(is_stale_digest(20, 30, self._at(20, 55)))  # 25 minutes late: still sent

    def test_hours_late_is_stale(self):
        from orders.tasks import is_stale_digest

        self.assertTrue(is_stale_digest(20, 30, self._at(21, 5)))  # 35 minutes
        self.assertTrue(is_stale_digest(20, 30, self._at(7, 0)))  # the next morning's startup
        self.assertTrue(is_stale_digest(20, 30, self._at(20, 29)))  # before today's time = yesterday's digest

    def test_a_late_digest_that_crosses_midnight_is_still_on_time(self):
        from orders.tasks import is_stale_digest

        self.assertFalse(is_stale_digest(23, 50, self._at(0, 10, day=14)))

    def _run(self, task_name, now, **kwargs):
        from orders import tasks

        with patch("orders.tasks._local_now", return_value=now), patch(
            "orders.tasks._build_restock_message", return_value="Item x1"
        ), patch("orders.tasks.restock.restock_message", return_value="Item x1"), patch(
            "orders.tasks.send_telegram_message"
        ) as telegram, patch("orders.tasks.logger"):
            getattr(tasks, task_name)(**kwargs)
        return telegram

    def test_a_stale_third_st_digest_is_not_sent(self):
        self._run("send_third_st_restock", self._at(7, 0), hour=20, minute=30).assert_not_called()

    def test_a_stale_delivery_digest_is_not_sent(self):
        self._run("send_delivery_restock", self._at(7, 0), hour=22, minute=0).assert_not_called()

    def test_an_on_time_digest_is_sent(self):
        self._run("send_third_st_restock", self._at(20, 31), hour=20, minute=30).assert_called_once()
        self._run("send_delivery_restock", self._at(22, 2), hour=22, minute=0).assert_called_once()

    def test_a_digest_run_by_hand_is_always_sent(self):
        self._run("send_third_st_restock", self._at(7, 0)).assert_called_once()

    def test_every_scheduled_digest_is_told_the_time_it_is_scheduled_for(self):
        schedule = {k: v for k, v in settings.CELERY_BEAT_SCHEDULE.items() if "restock" in k}
        self.assertEqual(len(schedule), 3)
        for name, entry in schedule.items():
            self.assertEqual({entry["kwargs"]["hour"]}, entry["schedule"].hour, name)
            self.assertEqual({entry["kwargs"]["minute"]}, entry["schedule"].minute, name)

    def test_the_real_clock_works_under_the_project_settings(self):
        # no mocked clock: with USE_TZ off, timezone.localtime() raises, which would have made every
        # scheduled digest crash at its scheduled time
        from orders import tasks
        from orders.tasks import is_stale_digest

        self.assertIn(is_stale_digest(20, 30), (True, False))
        with patch("orders.tasks._build_restock_message", return_value="Item x1"), patch(
            "orders.tasks.restock.restock_message", return_value="Item x1"
        ), patch("orders.tasks.send_telegram_message"), patch("orders.tasks.logger"):
            tasks.send_third_st_restock(hour=20, minute=30)
            tasks.send_delivery_restock(hour=22, minute=0)
