"""Phase 6: failures are visible and never double-send.

A scraper raises BeforeCommitError when it fails before its irreversible click,
and the Celery tasks retry exactly those. Anything else may have gone through, so
it is not retried automatically. Final failures land on the order (last_error) and
in the log; nothing here uses Telegram.
"""
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from orders.models import Driver, Notice, Order
from orders.services.scrapers.errors import BeforeCommitError, at_commit, before_commit, fail
from orders.tasks import RETRYABLE_TASKS, failure_text

WJ = "1111-2222-3333333-9001"
BASE = datetime(2026, 7, 20, 14, 0, 0)


def _as_dispatcher(request):
    request.user = User(username="dispatcher")
    return request


def _order(**extra):
    fields = dict(
        order_id="9001", webjoint_id=WJ, customer_name="Hailey White", customer_phone="15305551234",
        city="Davis", order_date=BASE, eta_start=BASE + timedelta(minutes=30),
        eta_end=BASE + timedelta(minutes=90), payment_type="Cash", total=Decimal("50.00"),
    )
    fields.update(extra)
    return Order.objects.create(**fields)


def _boom(step="compose message"):
    return BeforeCommitError(step, RuntimeError("boom"))


class TestScraperErrorClassification(SimpleTestCase):
    def test_before_commit_wraps_any_error_and_keeps_the_step(self):
        with self.assertRaises(BeforeCommitError) as caught:
            with before_commit("compose message"):
                raise ValueError("nope")
        self.assertEqual(caught.exception.step, "compose message")
        self.assertIsInstance(caught.exception.cause, ValueError)

    def test_before_commit_does_not_rewrap_an_inner_step(self):
        with self.assertRaises(BeforeCommitError) as caught:
            with before_commit("outer"):
                with before_commit("inner"):
                    raise ValueError("nope")
        self.assertEqual(caught.exception.step, "inner")

    def test_at_commit_reraises_the_original_error_tagged_with_its_step(self):
        original = ValueError("send failed")
        with self.assertRaises(ValueError) as caught:
            with at_commit("send message"):
                raise original
        self.assertIs(caught.exception, original)
        self.assertNotIsInstance(caught.exception, BeforeCommitError)
        self.assertEqual(caught.exception.step, "send message")

    def test_fail_before_any_commit_is_retryable(self):
        with self.assertRaises(BeforeCommitError):
            fail("row 0", ValueError("x"), committed=False)

    def test_fail_after_a_commit_is_never_retryable(self):
        with self.assertRaises(ValueError) as caught:
            fail("row 1", ValueError("x"), committed=True)
        self.assertNotIsInstance(caught.exception, BeforeCommitError)
        self.assertEqual(caught.exception.step, "row 1")

    def test_a_before_commit_error_seen_after_a_commit_is_converted_to_a_plain_error(self):
        with self.assertRaises(RuntimeError) as caught:
            fail("open Kits", _boom("open Kits"), committed=True)
        self.assertNotIsInstance(caught.exception, BeforeCommitError)
        self.assertEqual(caught.exception.step, "open Kits")


class TestFailureText(TestCase):
    def test_names_the_action_customer_first_name_and_last_initial_order_and_step(self):
        text = failure_text("Dispatch text", _boom(), _order())
        self.assertEqual(text, "Dispatch text failed for Hailey W. (order 9001) at step 'compose message': RuntimeError: boom")

    def test_never_includes_the_full_surname_or_phone_number(self):
        text = failure_text("Dispatch text", _boom(), _order())
        self.assertNotIn("White", text)
        self.assertNotIn("15305551234", text)

    def test_single_word_name(self):
        self.assertIn("for Cher (order", failure_text("WebJoint note", _boom(), _order(customer_name="Cher")))

    def test_failure_after_the_commit_warns_it_may_have_gone_through(self):
        exc = RuntimeError("click failed")
        exc.step = "send message"
        order = _order()
        text = failure_text("Dispatch text", exc, order)
        self.assertIn("at step 'send message'", text)
        self.assertIn("may have gone through", text)
        self.assertNotIn("may have gone through", failure_text("Dispatch text", _boom(), order))

    def test_multiline_selenium_messages_are_cut_to_one_line(self):
        exc = BeforeCommitError("compose message", RuntimeError("Message: no such element\nStacktrace:\n  #0 ..."))
        self.assertNotIn("Stacktrace", failure_text("Dispatch text", exc, _order()))


class TestTasksRetryOnlyBeforeTheCommit(TestCase):
    def setUp(self):
        _order()

    def _run(self, effect):
        from orders.tasks import fill_order_notes_task
        calls = []

        def scraper(*args):
            calls.append(args)
            return effect(len(calls))

        with patch("orders.tasks.fill_order_notes", side_effect=scraper), self.assertLogs("orders.tasks", "ERROR") as logs:
            result = fill_order_notes_task.apply(args=[WJ, "a note"])
            logger_lines = list(logs.output)
        return result, calls, logger_lines

    def _raise(self, exc):
        def effect(call_number):
            raise exc
        return effect

    def test_retry_policy(self):
        from orders.tasks import fill_order_notes_task
        self.assertEqual(fill_order_notes_task.autoretry_for, (BeforeCommitError,))
        self.assertEqual((fill_order_notes_task.retry_backoff, fill_order_notes_task.max_retries), (30, 2))
        self.assertFalse(fill_order_notes_task.acks_late)

    def test_a_before_commit_failure_is_retried_twice_then_saved_on_the_order(self):
        result, calls, lines = self._run(self._raise(_boom()))
        self.assertEqual(len(calls), 3)  # the first try plus 2 retries
        order = Order.objects.get(order_id="9001")
        self.assertIn("WebJoint note failed for Hailey W. (order 9001) at step 'compose message'", order.last_error)
        self.assertFalse(order.last_error_after_commit)
        self.assertIsNotNone(order.last_error_at)
        self.assertEqual(order.last_error_task, "orders.tasks.fill_order_notes_task")
        self.assertEqual(order.last_error_args, {"args": [WJ, "a note"], "kwargs": {}})
        self.assertTrue(any("WebJoint note failed" in line for line in lines))

    def test_a_failure_at_or_after_the_commit_is_never_retried(self):
        exc = RuntimeError("click failed")
        exc.step = "save notes"
        result, calls, _ = self._run(self._raise(exc))
        self.assertEqual(len(calls), 1)
        order = Order.objects.get(order_id="9001")
        self.assertTrue(order.last_error_after_commit)
        self.assertIn("at step 'save notes'", order.last_error)
        self.assertIn("may have gone through", order.last_error)

    def test_success_on_a_retry_leaves_no_error(self):
        def effect(call_number):
            if call_number == 1:
                raise _boom()
        with patch("orders.tasks.fill_order_notes", side_effect=lambda *a: effect(next(counter))) as scraper:
            counter = iter(range(1, 10))
            from orders.tasks import fill_order_notes_task
            result = fill_order_notes_task.apply(args=[WJ, "a note"])
        self.assertEqual(scraper.call_count, 2)
        self.assertEqual(Order.objects.get(order_id="9001").last_error, "")
        self.assertTrue(result.successful())

    def test_a_task_with_no_order_raises_a_dashboard_notice_instead(self):
        from orders.tasks import create_product_task
        with patch("orders.tasks.create_product", side_effect=_boom("open Products")), \
             self.assertLogs("orders.tasks", "ERROR"):
            create_product_task.apply(args=[[{"product_name": "X"}]])
        notice = Notice.objects.get()
        self.assertEqual(notice.level, "error")
        self.assertIn("Product creation failed", notice.message)
        self.assertIn("open Products", notice.message)

    def test_an_unknown_order_is_still_reported(self):
        from orders.tasks import fill_order_notes_task
        with patch("orders.tasks.fill_order_notes", side_effect=_boom()), self.assertLogs("orders.tasks", "ERROR"):
            fill_order_notes_task.apply(args=["no-such-webjoint-id", "note"])
        self.assertIn("no-such-webjoint-id", Notice.objects.get().message)


class TestPrepareOrderTaskNeverRepeatsAStep(TestCase):
    def _run(self, pull=None, dispatch=None, dispatch_flag=True):
        from orders.tasks import prepare_order_task
        with patch("orders.tasks.pull_from_chiles", side_effect=pull) as pull_mock, \
             patch("orders.tasks.dispatch_to_driver", side_effect=dispatch) as dispatch_mock, \
             patch("orders.tasks.pull_from_chiles_task") as pull_task, \
             patch("orders.tasks.dispatch_to_driver_task") as dispatch_task, \
             self.assertLogs("orders.tasks", "ERROR") if (pull or dispatch) else patch("orders.tasks.logger"):
            result = prepare_order_task.apply(args=[WJ, dispatch_flag])
        return result, pull_mock, dispatch_mock, pull_task, dispatch_task

    def test_a_pull_that_failed_before_its_commit_is_handed_to_its_own_retrying_task(self):
        result, pull, dispatch, pull_task, dispatch_task = self._run(pull=_boom("open order"))
        dispatch.assert_called_once_with(WJ)          # the dispatch still ran
        pull_task.delay.assert_called_once_with(WJ)   # the pull retries on its own
        dispatch_task.delay.assert_not_called()
        self.assertEqual(pull.call_count, 1)          # this task did not retry the dispatch
        self.assertTrue(result.successful())

    def test_a_dispatch_that_failed_before_its_commit_is_handed_off_without_repeating_the_pull(self):
        result, pull, dispatch, pull_task, dispatch_task = self._run(dispatch=_boom("assign driver"))
        pull.assert_called_once_with(WJ)
        dispatch_task.delay.assert_called_once_with(WJ)
        pull_task.delay.assert_not_called()
        self.assertEqual(dispatch.call_count, 1)

    def test_a_failure_after_the_commit_is_raised_and_not_handed_off(self):
        exc = RuntimeError("click failed")
        exc.step = "confirm dispatch"
        result, pull, dispatch, pull_task, dispatch_task = self._run(dispatch=exc)
        dispatch_task.delay.assert_not_called()
        self.assertEqual(dispatch.call_count, 1)
        self.assertTrue(result.failed())

    def test_nothing_is_dispatched_when_not_asked(self):
        result, pull, dispatch, *_ = self._run(dispatch_flag=False)
        dispatch.assert_not_called()


class TestSendMessageTask(TestCase):
    def test_success_moves_a_sending_order_to_sent(self):
        from orders.tasks import send_message_task
        _order(status=Order.Status.SENDING)
        with patch("orders.tasks.send_message") as scraper:
            send_message_task.apply(args=["9001", "15305551234", "hello"])
        scraper.assert_called_once_with("15305551234", "hello", order_ref="9001")
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.SENT)

    def test_a_dismissed_or_changed_order_is_not_pushed_back_to_sent(self):
        from orders.tasks import send_message_task
        _order(status=Order.Status.COMPLETED)
        with patch("orders.tasks.send_message"):
            send_message_task.apply(args=["9001", "15305551234", "hello"])
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.COMPLETED)

    def test_failure_leaves_the_order_sending_with_the_error_and_no_phone_in_it(self):
        from orders.tasks import send_message_task
        _order(status=Order.Status.SENDING)
        with patch("orders.tasks.send_message", side_effect=_boom()), self.assertLogs("orders.tasks", "ERROR"):
            send_message_task.apply(args=["9001", "15305551234", "hello"])
        order = Order.objects.get(order_id="9001")
        self.assertEqual(order.status, Order.Status.SENDING)
        self.assertIn("Dispatch text failed for Hailey W.", order.last_error)
        self.assertNotIn("15305551234", order.last_error)

    def test_a_text_that_failed_after_the_send_click_is_not_retried(self):
        from orders.tasks import send_message_task
        _order(status=Order.Status.SENDING)
        exc = RuntimeError("send click failed")
        exc.step = "send message"
        with patch("orders.tasks.send_message", side_effect=exc) as scraper, self.assertLogs("orders.tasks", "ERROR"):
            send_message_task.apply(args=["9001", "15305551234", "hello"])
        self.assertEqual(scraper.call_count, 1)


class TestSendingStatus(TestCase):
    def setUp(self):
        self.driver = Driver.objects.create(name="D1", is_active=True)

    def _send(self, view_name="send_dispatch_msg_view", **extra):
        from orders import views
        request = RequestFactory().post("/x", {"order_id": "9001", "eta_start": "14:45", "eta_end": "15:45", **extra})
        with patch("orders.tasks.send_message_task") as send_task, patch("orders.tasks.fill_order_notes_task"):
            getattr(views, view_name)(_as_dispatcher(request))
        return send_task

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_send_sets_sending_before_the_task_runs(self):
        _order(driver=self.driver)
        statuses = []
        from orders import views
        request = RequestFactory().post("/x", {"order_id": "9001", "eta_start": "14:45", "eta_end": "15:45"})
        with patch("orders.tasks.send_message_task") as send_task, patch("orders.tasks.fill_order_notes_task"):
            send_task.delay.side_effect = lambda *a: statuses.append(Order.objects.get(order_id="9001").status)
            views.send_dispatch_msg_view(_as_dispatcher(request))
        self.assertEqual(statuses, [Order.Status.SENDING])
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.SENDING)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_a_second_send_cannot_text_the_customer_again(self):
        _order(driver=self.driver)
        first = self._send()
        first.delay.assert_called_once()
        with self.assertRaises(Http404):
            self._send()

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_reschedule_sets_sending_too_and_blocks_a_second_send(self):
        _order(driver=self.driver)
        self._send("send_reschedule_msg_view")
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.SENDING)
        with self.assertRaises(Http404):
            self._send("send_reschedule_msg_view")

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_test_mode_texts_nobody_and_goes_straight_to_sent(self):
        _order(driver=self.driver)
        send_task = self._send()
        send_task.delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.SENT)


class TestErrorCard(TestCase):
    def _html(self):
        from orders.views import pending_orders_partial
        return pending_orders_partial(_as_dispatcher(RequestFactory().get("/p"))).content.decode()

    def setUp(self):
        self.driver = Driver.objects.create(name="D1", is_active=True)

    def test_error_shows_in_red_with_retry_and_dismiss(self):
        _order(driver=self.driver, status=Order.Status.SENDING, last_error="Dispatch text failed for Hailey W.",
               last_error_task="orders.tasks.send_message_task", last_error_args={"args": ["9001", "1", "x"], "kwargs": {}})
        html = self._html()
        self.assertIn("dispatch-error", html)
        self.assertIn("Dispatch text failed for Hailey W.", html)
        self.assertIn('action="/retry_task"', html)
        self.assertIn('action="/dismiss_error"', html)
        self.assertIn("Sending the dispatch text", html)
        self.assertNotIn('action="/send_dispatch_msg"', html)  # no second Send on a sending order
        self.assertNotIn("confirm(", html)  # failed before the commit: a plain retry

    def test_retry_after_the_commit_asks_for_a_confirm(self):
        _order(driver=self.driver, status=Order.Status.SENDING, last_error="failed", last_error_after_commit=True,
               last_error_task="orders.tasks.send_message_task")
        self.assertIn("confirm(", self._html())

    def test_an_already_sent_order_with_a_failed_note_still_shows(self):
        _order(driver=self.driver, status=Order.Status.SENT, last_error="WebJoint note failed")
        html = self._html()
        self.assertIn("WebJoint note failed", html)
        self.assertNotIn('action="/send_dispatch_msg"', html)

    def test_a_healthy_sent_order_is_not_in_the_queue(self):
        _order(driver=self.driver, status=Order.Status.SENT)
        self.assertNotIn("Hailey White", self._html())

    def test_a_sending_order_without_an_error_shows_as_sending(self):
        _order(driver=self.driver, status=Order.Status.SENDING, order_date=datetime.now())
        html = self._html()
        self.assertIn("Sending the dispatch text", html)
        self.assertNotIn("dispatch-error", html)
        # if a worker died mid-send there is no error to dismiss; the card still has a way out
        self.assertIn("Mark as sent", html)


class TestRetryAndDismissViews(TestCase):
    def setUp(self):
        self.driver = Driver.objects.create(name="D1", is_active=True)

    def _post(self, view_name, **data):
        from orders import views
        return getattr(views, view_name)(_as_dispatcher(RequestFactory().post("/x", data)))

    def _failed(self, **extra):
        fields = dict(
            driver=self.driver, status=Order.Status.SENDING, last_error="failed", last_error_after_commit=True,
            last_error_at=BASE, last_error_task="orders.tasks.send_message_task",
            last_error_args={"args": ["9001", "15305551234", "hello"], "kwargs": {}},
        )
        fields.update(extra)
        return _order(**fields)

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_retry_requeues_the_same_task_with_the_same_arguments_and_clears_the_error(self):
        self._failed()
        with patch.dict(RETRYABLE_TASKS, {"orders.tasks.send_message_task": MagicMock()}) as tasks:
            self._post("retry_task_view", order_id="9001")
            tasks["orders.tasks.send_message_task"].delay.assert_called_once_with("9001", "15305551234", "hello")
        order = Order.objects.get(order_id="9001")
        self.assertEqual((order.last_error, order.last_error_task, order.last_error_after_commit), ("", "", False))
        self.assertIsNone(order.last_error_at)
        self.assertEqual(order.status, Order.Status.SENDING)  # still waiting on the retried text

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_retry_keeps_keyword_arguments(self):
        self._failed(last_error_task="orders.tasks.resolve_payment_type_task",
                     last_error_args={"args": ["9001"], "kwargs": {"write_notes": True}})
        with patch.dict(RETRYABLE_TASKS, {"orders.tasks.resolve_payment_type_task": MagicMock()}) as tasks:
            self._post("retry_task_view", order_id="9001")
            tasks["orders.tasks.resolve_payment_type_task"].delay.assert_called_once_with("9001", write_notes=True)

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_retry_in_test_mode_queues_nothing(self):
        self._failed()
        with patch.dict(RETRYABLE_TASKS, {"orders.tasks.send_message_task": MagicMock()}) as tasks:
            self._post("retry_task_view", order_id="9001")
            tasks["orders.tasks.send_message_task"].delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="9001").last_error, "")

    def test_retry_only_runs_known_tasks(self):
        self._failed(last_error_task="os.system")
        with self.assertRaises(Http404):
            self._post("retry_task_view", order_id="9001")
        self.assertEqual(Order.objects.get(order_id="9001").last_error, "failed")

    def test_retry_without_an_error_is_a_404(self):
        _order(driver=self.driver)
        with self.assertRaises(Http404):
            self._post("retry_task_view", order_id="9001")

    def test_every_order_task_is_retryable_by_name(self):
        self.assertEqual(
            sorted(RETRYABLE_TASKS),
            sorted(f"orders.tasks.{n}" for n in (
                "send_message_task", "fill_order_notes_task", "pull_from_chiles_task",
                "dispatch_to_driver_task", "prepare_order_task", "resolve_payment_type_task",
            )),
        )

    def test_dismiss_clears_the_error_and_marks_a_stuck_sending_order_sent(self):
        self._failed()
        self._post("dismiss_error_view", order_id="9001")
        order = Order.objects.get(order_id="9001")
        self.assertEqual((order.last_error, order.status), ("", Order.Status.SENT))

    def test_a_stuck_sending_order_with_no_error_can_be_marked_sent(self):
        _order(driver=self.driver, status=Order.Status.SENDING)
        self._post("dismiss_error_view", order_id="9001")
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.SENT)

    def test_dismiss_on_a_healthy_order_is_a_404(self):
        _order(driver=self.driver, status=Order.Status.SENT)
        with self.assertRaises(Http404):
            self._post("dismiss_error_view", order_id="9001")

    def test_dismiss_does_not_change_the_status_of_other_orders(self):
        self._failed(status=Order.Status.COMPLETED)
        self._post("dismiss_error_view", order_id="9001")
        self.assertEqual(Order.objects.get(order_id="9001").status, Order.Status.COMPLETED)

    def test_dismiss_notice(self):
        from orders.views import dismiss_notice_view
        notice = Notice.objects.create(message="hello")
        dismiss_notice_view(_as_dispatcher(RequestFactory().post("/x", {"notice_id": notice.pk})))
        notice.refresh_from_db()
        self.assertTrue(notice.dismissed)


class TestNoticesOnTheDashboard(TestCase):
    def test_active_notices_are_shown_and_dismissed_ones_are_not(self):
        Notice.objects.create(message="Send merchant pay link to Hailey W. for $12.00", level="info")
        Notice.objects.create(message="Webhook registration FAILED", level="error")
        Notice.objects.create(message="old news", dismissed=True)
        client = Client()
        client.force_login(User.objects.create_user("dispatcher"))
        html = client.get("/").content.decode()
        self.assertIn("Send merchant pay link to Hailey W. for $12.00", html)
        self.assertIn("Webhook registration FAILED", html)
        self.assertNotIn("old news", html)
        self.assertIn('action="/dismiss_notice"', html)


class TestMerchantPayReminder(TestCase):
    def test_posts_a_notice_with_the_card_fee_total_and_opens_no_browser(self):
        from orders.tasks import send_merchant_pay_link_task
        _order(total=Decimal("100.00"))
        with patch("orders.services.scrapers.browser.create_driver") as chrome, self.assertLogs("orders.tasks", "INFO"):
            send_merchant_pay_link_task.apply(args=["9001"])
        chrome.assert_not_called()
        # 100.00 + 2.25% + 0.35 = 102.60
        self.assertEqual(Notice.objects.get().message, "Send merchant pay link to Hailey W. for $102.60 (order 9001)")

    def test_uses_the_combined_total_of_a_linked_order(self):
        from orders.tasks import send_merchant_pay_link_task
        primary = _order(total=Decimal("100.00"), delivery_fee=Decimal("5.00"))
        _order(order_id="9002", webjoint_id="x", customer_name="Fsa", linked_order=primary,
               total=Decimal("50.00"), delivery_fee=Decimal("3.00"))
        with self.assertLogs("orders.tasks", "INFO"):
            send_merchant_pay_link_task.apply(args=["9001"])
        # 100 + 50 - min(5, 3) = 147.00; + 2.25% + 0.35 = 150.66
        self.assertIn("$150.66", Notice.objects.get().message)

    def test_unknown_order_is_a_logged_noop(self):
        from orders.tasks import send_merchant_pay_link_task
        with self.assertLogs("orders.tasks", "WARNING"):
            send_merchant_pay_link_task.apply(args=["nope"])
        self.assertEqual(Notice.objects.count(), 0)


class TestFailureArtifacts(SimpleTestCase):
    """browser_session saves a screenshot + page source on any failure and prunes old ones."""

    def setUp(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        override = override_settings(FAILURE_DIR=folder.name)
        override.enable()
        self.addCleanup(override.disable)

    def _driver(self):
        driver = MagicMock()
        driver.save_screenshot.side_effect = lambda path: Path(path).write_bytes(b"png") or True
        driver.page_source = "<html>page</html>"
        return driver

    def _session(self, driver, label="send_message", ref="9001", fail_with=ValueError("boom")):
        from orders.services.scrapers import browser
        with patch.object(browser, "create_driver", return_value=(driver, MagicMock())), \
             patch.object(browser, "_now", return_value=datetime(2026, 7, 20, 14, 0, 0)):
            try:
                with browser.browser_session(label, ref):
                    if fail_with:
                        raise fail_with
            except Exception as exc:
                return exc

    def test_failure_writes_png_and_html_named_for_task_order_and_time(self):
        driver = self._driver()
        with self.assertLogs("orders.services.scrapers.browser", "ERROR"):
            exc = self._session(driver)
        self.assertIsInstance(exc, ValueError)  # the original error still propagates
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["send_message-9001-20260720-140000.html", "send_message-9001-20260720-140000.png"],
        )
        self.assertEqual((self.dir / "send_message-9001-20260720-140000.html").read_text(), "<html>page</html>")
        driver.quit.assert_called_once()

    def test_success_writes_nothing_and_still_quits(self):
        driver = self._driver()
        self._session(driver, fail_with=None)
        self.assertEqual(list(self.dir.iterdir()), [])
        driver.quit.assert_called_once()

    def test_files_older_than_14_days_are_pruned_when_writing(self):
        old, recent = self.dir / "old.png", self.dir / "recent.png"
        for f in (old, recent):
            f.write_bytes(b"x")
        old_time = (datetime(2026, 7, 20, 14) - timedelta(days=15)).timestamp()
        recent_time = (datetime(2026, 7, 20, 14) - timedelta(days=13)).timestamp()
        os.utime(old, (old_time, old_time))
        os.utime(recent, (recent_time, recent_time))
        with self.assertLogs("orders.services.scrapers.browser", "ERROR"):
            self._session(self._driver())
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_a_broken_screenshot_never_replaces_the_real_error(self):
        driver = self._driver()
        driver.save_screenshot.side_effect = RuntimeError("no display")
        with self.assertLogs("orders.services.scrapers.browser"):
            exc = self._session(driver)
        self.assertIsInstance(exc, ValueError)
        self.assertEqual([p.suffix for p in self.dir.iterdir()], [".html"])
        driver.quit.assert_called_once()

    def test_unsafe_characters_in_the_order_reference_cannot_escape_the_folder(self):
        with self.assertLogs("orders.services.scrapers.browser", "ERROR"):
            self._session(self._driver(), ref="../../etc/passwd")
        self.assertTrue(all(p.parent == self.dir for p in self.dir.iterdir()))

    def test_chrome_failing_to_start_is_a_before_commit_error(self):
        from orders.services.scrapers import browser
        with patch.object(browser, "create_driver", side_effect=OSError("no chrome")):
            with self.assertRaises(BeforeCommitError) as caught:
                with browser.browser_session("send_message", "9001"):
                    pass
        self.assertEqual(caught.exception.step, "start browser")

    def test_failures_dir_defaults_under_the_gitignored_logs_folder(self):
        from TGPFflows import settings as real_settings
        self.assertEqual(Path(real_settings.FAILURE_DIR), Path(real_settings.BASE_DIR) / "logs" / "failures")


class TestFetch2faCode(SimpleTestCase):
    def test_imap_credentials_come_from_settings(self):
        from orders.services.scrapers import merchant_pay_helper
        mailbox = MagicMock()
        mailbox.__enter__.return_value = mailbox
        mailbox.search.return_value = ("OK", [b"1"])
        message = b"Subject: code\r\n\r\nYour code is 123456\r\n"
        mailbox.fetch.return_value = ("OK", [(b"1", message)])
        with override_settings(MERCHANT_PAY_IMAP_USER="me@example.com", MERCHANT_PAY_IMAP_PASS="secret"), \
             patch.object(merchant_pay_helper.imaplib, "IMAP4_SSL", return_value=mailbox):
            code = merchant_pay_helper.fetch_2fa_code(timeout=1, poll=0)
        mailbox.login.assert_called_once_with("me@example.com", "secret")
        self.assertEqual(code, "123456")


class TestStartupWebhookNotice(TestCase):
    def _start(self, effect):
        from orders.apps import OrdersConfig
        config = OrdersConfig("orders", __import__("orders"))
        with patch("orders.management.commands.start_ngrok.start_ngrok", **effect):
            config._start_tunnel_and_register_webhooks()

    def test_success_is_logged_and_shown_as_an_info_notice(self):
        with self.assertLogs("orders.apps", "INFO") as logs:
            self._start({"return_value": "https://abc.ngrok.app"})
        notice = Notice.objects.get()
        self.assertEqual(notice.level, "info")
        self.assertIn("https://abc.ngrok.app", notice.message)
        self.assertTrue(any("Webhooks registered" in line for line in logs.output))

    def test_failure_is_logged_shown_as_an_error_notice_and_does_not_crash_startup(self):
        with self.assertLogs("orders.apps", "ERROR"):
            self._start({"side_effect": BeforeCommitError("save webhooks", RuntimeError("no save button"))})
        notice = Notice.objects.get()
        self.assertEqual(notice.level, "error")
        self.assertIn("FAILED", notice.message)
        self.assertIn("no save button", notice.message)


class TestNoTelegramOutsideTheRestockDigests(SimpleTestCase):
    def test_only_the_restock_tasks_use_telegram(self):
        root = Path(settings.BASE_DIR) / "orders"
        users = sorted(
            str(p.relative_to(root)) for p in root.rglob("*.py")
            if "telegram" in p.read_text().lower() and "tests" not in p.name and "migrations" not in p.parts
        )
        # send_test_message is the manual "does the channel work" command; it refuses in test mode
        self.assertEqual(users, ["management/commands/send_test_message.py", "services/telegram.py", "tasks.py"])
        source = (root / "tasks.py").read_text()
        self.assertEqual(source.count("send_telegram_message("), 2)
