from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


@override_settings(RESTOCK_TEST_MODE=True, TALKROUTE_TEST_PHONE="5305550100")
class TestSendTestMessageInTestMode(SimpleTestCase):
    def test_refuses_to_send_anything(self):
        with patch("orders.services.scrapers.talkroute.send_message") as sms, patch(
            "orders.services.telegram.send_telegram_message"
        ) as telegram:
            for flag in ("--sms", "--telegram"):
                with self.assertRaises(CommandError):
                    call_command("send_test_message", flag)
        sms.assert_not_called()
        telegram.assert_not_called()


@override_settings(RESTOCK_TEST_MODE=False)
class TestSendTestMessage(SimpleTestCase):
    @override_settings(TALKROUTE_TEST_PHONE="5305550100")
    def test_sms_texts_the_test_phone(self):
        with patch("orders.services.scrapers.talkroute.send_message") as sms:
            call_command("send_test_message", "--sms")
        sms.assert_called_once_with("5305550100", "testing", order_ref="test")

    @override_settings(TALKROUTE_TEST_PHONE="")
    def test_sms_needs_a_test_phone(self):
        with self.assertRaises(CommandError):
            call_command("send_test_message", "--sms")

    def test_telegram_posts_to_the_channel(self):
        with patch("orders.services.telegram.send_telegram_message") as telegram:
            call_command("send_test_message", "--telegram")
        telegram.assert_called_once_with("testing")

    def test_a_channel_is_required(self):
        with self.assertRaises(CommandError):
            call_command("send_test_message")
