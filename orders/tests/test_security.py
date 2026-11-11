"""Tests for the webhook secret, dashboard login and CSRF protection."""

import json
import logging
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase

from orders.webhooks import RedactWebhookKeyFilter, redact_webhook_key, webhook_urls

KEY = settings.WEBHOOK_SECRET

WEBHOOK_NAMES = ("new_order", "complete_order", "cancel_order")

# every dashboard view that changes something (all POST-only)
DASHBOARD_POST_PATHS = (
    "/clear_dispatch_queue",
    "/add_driver",
    "/toggle_driver",
    "/assign_driver",
    "/delete_driver",
    "/retry_task",
    "/dismiss_error",
    "/dismiss_notice",
    "/complete_order_manual",
    "/send_dispatch_msg",
    "/send_reschedule_msg",
    "/link_order",
    "/unlink_order",
    "/reconcile_restock",
    "/clear_restock",
    "/add_shell_order",
    "/add_products_from_invoice",
    "/set_driver_location",
)


def _post_json(client, path, payload=None):
    return client.post(path, data=json.dumps(payload or {"id": "9001"}), content_type="application/json")


class TestWebhookKey(TestCase):
    def test_wrong_key_is_forbidden_on_every_webhook(self):
        client = Client()
        for name in WEBHOOK_NAMES:
            with self.subTest(name=name), self.assertLogs("orders.webhooks", "WARNING") as logs:
                response = _post_json(client, f"/hooks/not-the-key/{name}")
                self.assertEqual(response.status_code, 403)
                self.assertIn("wrong key", logs.output[0])
                self.assertNotIn(KEY, logs.output[0])

    def test_wrong_key_does_not_reach_the_view(self):
        with patch("orders.views.cancel_order") as mock_cancel, self.assertLogs("orders.webhooks", "WARNING"):
            response = _post_json(Client(), "/hooks/not-the-key/cancel_order")
        self.assertEqual(response.status_code, 403)
        mock_cancel.assert_not_called()

    def test_key_of_different_length_or_non_ascii_is_forbidden(self):
        client = Client()
        for key in (KEY[:-1], KEY + "x", "kéy"):
            with self.subTest(key=key), self.assertLogs("orders.webhooks", "WARNING"):
                self.assertEqual(_post_json(client, f"/hooks/{key}/cancel_order").status_code, 403)

    def test_legacy_bare_paths_are_forbidden_and_logged(self):
        client = Client()
        for name in WEBHOOK_NAMES:
            with self.subTest(name=name), self.assertLogs("orders.views", "WARNING") as logs:
                response = _post_json(client, f"/{name}")
                self.assertEqual(response.status_code, 403)
                self.assertIn(f"/{name}", logs.output[0])

    def test_legacy_path_forbidden_even_with_csrf_enforced(self):
        with self.assertLogs("orders.views", "WARNING"):
            response = _post_json(Client(enforce_csrf_checks=True), "/new_order")
        self.assertEqual(response.status_code, 403)

    def test_correct_key_reaches_the_view(self):
        with patch("orders.views.cancel_order") as mock_cancel:
            response = _post_json(Client(), f"/hooks/{KEY}/cancel_order", {"id": "9001"})
        self.assertEqual(response.status_code, 200)
        mock_cancel.assert_called_once_with(order_id="9001")

    def test_correct_key_works_without_a_csrf_token(self):
        with patch("orders.views.cancel_order"):
            response = _post_json(Client(enforce_csrf_checks=True), f"/hooks/{KEY}/cancel_order")
        self.assertEqual(response.status_code, 200)

    def test_complete_order_webhook_with_correct_key(self):
        with patch("orders.views.complete_order") as mock_complete:
            response = _post_json(Client(), f"/hooks/{KEY}/complete_order", {"id": 9001, "shipping": {"city": "Davis"}})
        self.assertEqual(response.status_code, 200)
        mock_complete.assert_called_once_with(order_id="9001", city="Davis")

    def test_webhook_rejects_get_even_with_correct_key(self):
        self.assertEqual(Client().get(f"/hooks/{KEY}/new_order").status_code, 405)


class TestWebhookHelpers(SimpleTestCase):
    def test_webhook_urls_contain_the_secret_and_strip_trailing_slash(self):
        urls = webhook_urls("https://abc.ngrok.app/")
        self.assertEqual(urls["new_order"], f"https://abc.ngrok.app/hooks/{KEY}/new_order")
        self.assertEqual(urls["complete_order"], f"https://abc.ngrok.app/hooks/{KEY}/complete_order")
        self.assertEqual(urls["cancel_order"], f"https://abc.ngrok.app/hooks/{KEY}/cancel_order")

    def test_redact_webhook_key(self):
        self.assertEqual(
            redact_webhook_key(f"POST /hooks/{KEY}/new_order HTTP/1.1"),
            "POST /hooks/<redacted>/new_order HTTP/1.1",
        )
        self.assertEqual(redact_webhook_key("GET /pending_orders_partial"), "GET /pending_orders_partial")

    def test_log_filter_masks_the_key_in_runserver_style_records(self):
        record = logging.makeLogRecord(
            {
                "msg": '"%s" %s %s',
                "args": (f"POST /hooks/{KEY}/new_order HTTP/1.1", "200", "15"),
            }
        )
        self.assertTrue(RedactWebhookKeyFilter().filter(record))
        self.assertNotIn(KEY, record.getMessage())
        self.assertIn("/hooks/<redacted>/new_order", record.getMessage())


class TestDashboardRequiresLogin(TestCase):
    def test_anonymous_dashboard_get_redirects_to_login(self):
        client = Client()
        for path in ("/", "/pending_orders_partial", "/restock_partial"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"/accounts/login/?next={path}")

    def test_anonymous_dashboard_post_redirects_to_login(self):
        client = Client()
        for path in DASHBOARD_POST_PATHS:
            with self.subTest(path=path):
                response = client.post(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith("/accounts/login/"))

    def test_login_page_renders(self):
        response = Client().get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_logging_in_goes_to_the_dashboard(self):
        User.objects.create_user("dispatcher", password="s3cret-pass")
        client = Client()
        response = client.post("/accounts/login/", {"username": "dispatcher", "password": "s3cret-pass"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Sign out")

    def test_wrong_password_shows_an_error_and_stays_logged_out(self):
        User.objects.create_user("dispatcher", password="s3cret-pass")
        client = Client()
        response = client.post("/accounts/login/", {"username": "dispatcher", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wrong username or password")
        self.assertEqual(client.get("/").status_code, 302)

    def test_logout_redirects_back_to_login(self):
        client = Client()
        client.force_login(User.objects.create_user("dispatcher"))
        response = client.post("/accounts/logout/")
        self.assertRedirects(response, "/accounts/login/", fetch_redirect_response=False)
        self.assertEqual(client.get("/").status_code, 302)


class TestDashboardCsrf(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(User.objects.create_user("dispatcher"))

    def test_post_without_csrf_token_is_forbidden(self):
        for path in (
            "/clear_restock",
            "/clear_dispatch_queue",
            "/add_driver",
            "/toggle_driver",
            "/assign_driver",
            "/retry_task",
            "/dismiss_error",
            "/dismiss_notice",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 403)

    def test_post_with_csrf_token_goes_through(self):
        # loading the dashboard sets the csrftoken cookie; echo it back as the form token
        self.client.get("/")
        token = self.client.cookies["csrftoken"].value
        response = self.client.post("/clear_restock", {"csrfmiddlewaretoken": token})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
