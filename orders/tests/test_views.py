"""Input handling in the views: bad webhook bodies, refused dashboard actions, invoice uploads."""
import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from anthropic import APIError
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings

from orders.models import Driver, Notice, Order
from orders.services import dashboard
from orders.services.customers import customer_label, split_name
from orders.tests.helpers import POS_ORDER

KEY = settings.WEBHOOK_SECRET


def _post_raw(path, body, client=None):
    return (client or Client()).post(path, data=body, content_type="application/json")


class TestNewOrderRejectsBadBodies(TestCase):
    URL = f"/hooks/{KEY}/new_order"

    def _assert_rejected(self, body):
        with self.assertLogs("orders.views", "WARNING"), \
             patch("orders.tasks.prepare_order_task") as prepare, patch("orders.tasks.send_message_task") as send:
            response = _post_raw(self.URL, body if isinstance(body, str) else json.dumps(body))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(Order.objects.count(), 0)
        prepare.delay.assert_not_called()
        send.delay.assert_not_called()

    def test_invalid_json(self):
        self._assert_rejected("{not json")

    def test_json_that_is_not_an_object(self):
        self._assert_rejected([1, 2, 3])

    def test_missing_order_id(self):
        self._assert_rejected({k: v for k, v in POS_ORDER.items() if k != "id"})

    def test_missing_customer_name(self):
        self._assert_rejected(dict(POS_ORDER, customer={"phone": "+15307239356"}))

    def test_delivery_order_without_a_phone(self):
        self._assert_rejected(dict(POS_ORDER, customer={"name": "Hailey White"}))

    def test_delivery_order_without_a_city(self):
        self._assert_rejected(dict(POS_ORDER, shipping={}))

    def test_a_bad_number_is_rejected_not_a_server_error(self):
        self._assert_rejected(dict(POS_ORDER, total="a lot"))

    def test_the_error_does_not_echo_customer_details(self):
        with self.assertLogs("orders.views", "WARNING") as logs:
            response = _post_raw(self.URL, json.dumps(dict(POS_ORDER, customer={"name": "Hailey White"})))
        self.assertNotIn("Hailey", response.content.decode() + "".join(logs.output))

    def test_a_blocked_address_is_ignored_even_when_the_rest_is_junk(self):
        body = {"shipping": {"number": "526", "street": "3rd Street", "zip": "95616", "city": "Elsewhere"}}
        self.assertEqual(_post_raw(self.URL, json.dumps(body)).status_code, 200)
        self.assertEqual(Order.objects.count(), 0)


class TestCompleteAndCancelRejectBadBodies(SimpleTestCase):
    def test_missing_id_is_a_400_and_nothing_is_called(self):
        for name in ("complete_order", "cancel_order"):
            with self.subTest(name), patch(f"orders.views.{name}") as action, self.assertLogs("orders.views", "WARNING"):
                response = _post_raw(f"/hooks/{KEY}/{name}", json.dumps({"shipping": {"city": "Davis"}}))
            self.assertEqual(response.status_code, 400)
            action.assert_not_called()

    def test_invalid_json_is_a_400(self):
        with patch("orders.views.cancel_order") as action, self.assertLogs("orders.views", "WARNING"):
            response = _post_raw(f"/hooks/{KEY}/cancel_order", "nope")
        self.assertEqual(response.status_code, 400)
        action.assert_not_called()

    def test_a_null_shipping_block_is_fine(self):
        with patch("orders.views.complete_order") as action:
            response = _post_raw(f"/hooks/{KEY}/complete_order", json.dumps({"id": 5, "shipping": None}))
        self.assertEqual(response.status_code, 200)
        action.assert_called_once_with(order_id="5", city="")


class DashboardCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_user("dispatcher"))

    def _order(self, order_id="9001", **fields):
        defaults = dict(
            customer_name="Jane Doe", customer_phone="15305551234", city="Davis",
            order_date=datetime.now(), payment_type="Cash", total=Decimal("50.00"),
        )
        return Order.objects.create(order_id=order_id, **{**defaults, **fields})


class TestRefusedActionsAreShown(DashboardCase):
    def _page_after(self, path, data):
        with self.assertLogs("orders.views", "WARNING"):
            response = self.client.post(path, data, follow=True)
        return response.content.decode()

    def test_sending_an_order_with_no_driver_says_why(self):
        self._order(eta_start=datetime.now(), eta_end=datetime.now())
        page = self._page_after("/send_dispatch_msg", {"order_id": "9001"})
        self.assertIn("has no driver yet", page)

    def test_a_bad_time_says_so_and_changes_nothing(self):
        driver = Driver.objects.create(name="D1", is_active=True)
        start = datetime(2026, 7, 20, 14, 30)
        order = self._order(driver=driver, eta_start=start, eta_end=start)
        with patch("orders.tasks.send_message_task") as send:
            page = self._page_after("/send_dispatch_msg", {"order_id": "9001", "eta_start": "soon", "eta_end": "later"})
        self.assertIn("not a valid time window", page)
        send.delay.assert_not_called()
        order.refresh_from_db()
        self.assertEqual((order.eta_start, order.status), (start, Order.Status.AWAITING_APPROVAL))

    def test_the_message_is_shown_once(self):
        self._order(eta_start=datetime.now(), eta_end=datetime.now())
        self._page_after("/send_dispatch_msg", {"order_id": "9001"})
        self.assertNotIn("has no driver yet", self.client.get("/").content.decode())

    def test_adding_a_stop_with_nobody_on_shift_says_so(self):
        page = self._page_after("/add_shell_order", {"city": "Davis"})
        self.assertIn("No driver is on shift", page)

    def test_adding_a_driver_with_no_name_says_so(self):
        page = self._page_after("/add_driver", {"name": "   "})
        self.assertIn("Enter a driver name", page)
        self.assertEqual(Driver.objects.count(), 0)

    def test_linking_without_picking_an_order_says_so(self):
        page = self._page_after("/link_order", {"order_id": "9001"})
        self.assertIn("Pick the order to link to", page)


class TestBadIdsAreNotServerErrors(DashboardCase):
    def test_non_numeric_ids_are_404(self):
        for path, field in (
            ("/toggle_driver", "driver_id"), ("/delete_driver", "driver_id"), ("/dismiss_notice", "notice_id"),
            ("/set_driver_location", "driver_id"), ("/assign_driver", "driver_id"),
        ):
            with self.subTest(path):
                data = {field: "abc", "city": "Davis", "order_id": "9001"}
                self.assertEqual(self.client.post(path, data).status_code, 404)

    def test_dismissing_a_missing_notice_is_404(self):
        self.assertEqual(self.client.post("/dismiss_notice", {"notice_id": "999"}).status_code, 404)

    def test_dismissing_a_notice_hides_it(self):
        notice = Notice.objects.create(message="hello")
        self.assertEqual(self.client.post("/dismiss_notice", {"notice_id": notice.pk}).status_code, 302)
        notice.refresh_from_db()
        self.assertTrue(notice.dismissed)


class TestInvoiceUpload(DashboardCase):
    def _upload(self, name="invoice.pdf", content=b"%PDF fake", content_type="application/pdf"):
        return self.client.post(
            "/add_products_from_invoice", {"invoice": SimpleUploadedFile(name, content, content_type=content_type)},
            follow=True,
        )

    @override_settings(RESTOCK_TEST_MODE=False)
    def test_a_valid_invoice_is_read_and_products_are_queued(self):
        with patch("orders.views.extract_products_from_invoice", return_value=[{"product_name": "X"}]) as read, \
             patch("orders.tasks.create_product_task") as create:
            response = self._upload()
        self.assertEqual(response.status_code, 200)
        read.assert_called_once_with(b"%PDF fake", "application/pdf")
        create.delay.assert_called_once_with([{"product_name": "X"}])

    @override_settings(RESTOCK_TEST_MODE=True)
    def test_test_mode_reads_the_invoice_but_does_not_write_to_webjoint(self):
        with patch("orders.views.extract_products_from_invoice", return_value=[{"product_name": "X"}]), \
             patch("orders.tasks.create_product_task") as create, self.assertLogs("orders.views", "WARNING"):
            response = self._upload()
        create.delay.assert_not_called()
        self.assertContains(response, "Test mode is on")

    def test_no_file(self):
        with self.assertLogs("orders.views", "WARNING"):
            response = self.client.post("/add_products_from_invoice", follow=True)
        self.assertContains(response, "Choose an invoice file first")

    def test_wrong_file_type_is_refused_before_the_api_is_called(self):
        with patch("orders.views.extract_products_from_invoice") as read, self.assertLogs("orders.views", "WARNING"):
            response = self._upload("notes.txt", b"hi", "text/plain")
        read.assert_not_called()
        self.assertContains(response, "must be a PDF or an image")

    def test_oversized_file_is_refused_before_the_api_is_called(self):
        with patch("orders.services.invoice.MAX_UPLOAD_BYTES", 10), \
             patch("orders.views.extract_products_from_invoice") as read, self.assertLogs("orders.views", "WARNING"):
            response = self._upload(content=b"x" * 11)
        read.assert_not_called()
        self.assertContains(response, "too large")

    def test_an_api_error_is_shown_not_a_server_error(self):
        from orders.services import invoice

        class FakeApiError(APIError):
            def __init__(self):
                Exception.__init__(self, "boom")

        with patch.object(invoice, "Anthropic") as client_class, \
             self.assertLogs("orders.services.invoice", "ERROR"), self.assertLogs("orders.views", "WARNING"):
            client_class.return_value.messages.parse.side_effect = FakeApiError()
            response = self._upload()
        self.assertContains(response, "Could not read the invoice")


class TestPendingCardsQueries(DashboardCase):
    def _queue_orders(self, count):
        driver = Driver.objects.create(name="D1", is_active=True)
        for i in range(count):
            self._order(
                f"q{i}", driver=driver, eta_start=datetime(2026, 7, 20, 14, i), eta_end=datetime(2026, 7, 20, 15, i),
                customer_name="Frank Sinatra Adult" if i % 2 else "Pat Q",
            )

    def test_the_number_of_queries_does_not_grow_with_the_number_of_cards(self):
        self._queue_orders(2)
        with self.assertNumQueries(2):
            self.assertEqual(len(dashboard.pending_cards()), 2)
        for i in range(2, 8):
            self._order(f"x{i}", eta_start=datetime(2026, 7, 20, 16, i), eta_end=datetime(2026, 7, 20, 17, i),
                        driver=Driver.objects.first(), customer_name="Frank Sinatra Medical")
        with self.assertNumQueries(2):
            self.assertEqual(len(dashboard.pending_cards()), 8)


class TestCustomerNames(SimpleTestCase):
    def test_split_name(self):
        self.assertEqual(split_name("hailey WHITE jr"), ("Hailey", "White"))
        self.assertEqual(split_name("Cher"), ("Cher", ""))
        self.assertEqual(split_name(""), ("", ""))

    def test_customer_label_never_shows_the_full_name(self):
        self.assertEqual(customer_label("hailey white"), "Hailey W.")
        self.assertEqual(customer_label("Cher"), "Cher")
        self.assertEqual(customer_label(""), "customer")


@override_settings(RESTOCK_TEST_MODE=True)
class TestScheduledWindowAtIntake(TestCase):
    """A customer's chosen delivery window is kept when it is no earlier than our estimate."""

    def _place(self, start_in, end_in):
        from datetime import timedelta

        from orders.services.time import parse_webhook_time
        Driver.objects.create(name="D1", is_active=True, current_city="Davis")
        def stamp(delta):
            return (datetime.utcnow() + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        cart = {"deliveryTiming": "Scheduled", "scheduledStart": stamp(start_in), "scheduledEnd": stamp(end_in)}
        body = dict(POS_ORDER, created=stamp(timedelta(0)), cart=cart, subtotal=500, total=500, discount=0)
        with patch("orders.tasks.prepare_order_task"):
            self.assertEqual(_post_raw(f"/hooks/{KEY}/new_order", json.dumps(body)).status_code, 200)
        return Order.objects.get(order_id="6767008"), parse_webhook_time(cart["scheduledStart"])

    def test_a_later_window_replaces_the_estimate(self):
        from datetime import timedelta
        order, wanted_start = self._place(timedelta(hours=5), timedelta(hours=6))
        self.assertEqual(order.eta_start, wanted_start)

    def test_a_window_that_ends_before_the_estimate_is_ignored(self):
        from datetime import timedelta
        order, wanted_start = self._place(timedelta(minutes=1), timedelta(minutes=10))
        self.assertNotEqual(order.eta_start, wanted_start)
        self.assertIsNotNone(order.eta_start)


class TestDashboardAssets(DashboardCase):
    def test_the_page_links_its_stylesheet_and_script(self):
        page = self.client.get("/").content.decode()
        self.assertIn('href="/static/orders/dashboard.css"', page)
        self.assertIn('src="/static/orders/dashboard.js"', page)
        self.assertNotIn("<style>", page)

    def test_the_static_files_exist(self):
        from django.contrib.staticfiles import finders
        for name in ("orders/dashboard.css", "orders/dashboard.js"):
            self.assertIsNotNone(finders.find(name), name)

    def test_the_script_is_told_where_to_poll(self):
        page = self.client.get("/").content.decode()
        for attribute, path in (
            ("data-pending-url", "/pending_orders_partial"), ("data-restock-url", "/restock_partial"),
            ("data-login-url", "/accounts/login/"),
        ):
            self.assertIn(f'{attribute}="{path}"', page)
