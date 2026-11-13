from celery import current_app
from django.conf import settings
from django.test import SimpleTestCase

from orders import tasks
from orders.services.scrapers.browser import create_driver
from orders.tests import RealBrowserBlocked


class TestTestsCannotTouchTheRealWorld(SimpleTestCase):
    def test_the_queue_is_in_memory(self):
        self.assertTrue(current_app.conf.broker_url.startswith("memory://"))
        self.assertTrue(tasks.fill_order_notes_task.app.conf.broker_url.startswith("memory://"))

    def test_delay_does_not_reach_a_real_broker(self):
        # would have raised (or queued a real job) with a real Redis URL
        tasks.fill_order_notes_task.delay("1111-2222-3333333-9001", "note")

    def test_a_real_chrome_cannot_be_started(self):
        with self.assertRaises(RealBrowserBlocked):
            create_driver()

    def test_the_block_is_not_swallowed_by_a_broad_except(self):
        # browser_session wraps start-up failures; the block must still come through as itself
        from orders.services.scrapers.browser import browser_session

        with self.assertRaises(RealBrowserBlocked), browser_session("probe"):
            pass

    def test_logins_are_dummies_and_services_are_off(self):
        for name in ("WEBJOINT", "TALKROUTE", "WEEDMAPS", "MERCHANT_PAY"):
            self.assertTrue(getattr(settings, f"{name}_EMAIL").startswith("test-"), name)
            self.assertTrue(getattr(settings, f"{name}_PASSWORD").startswith("test-"), name)
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "NGROK_AUTH_TOKEN",
            "GOOGLE_MAPS_API_KEY",
            "ANTHROPIC_API_KEY",
            "TALKROUTE_TEST_PHONE",
        ):
            self.assertEqual(getattr(settings, name), "", name)
