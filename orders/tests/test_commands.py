import subprocess
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings


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


class TestTalkrouteLogin(SimpleTestCase):
    """manage.py talkroute_login opens the saved profile in a plain Chrome and waits for it to be closed."""

    def _run(self, wait_side_effect):
        from io import StringIO

        process = MagicMock()
        process.wait.side_effect = wait_side_effect
        out = StringIO()
        popen_patch = patch("orders.management.commands.talkroute_login.subprocess.Popen", return_value=process)
        binary_patch = patch("orders.management.commands.talkroute_login.chrome_binary", return_value="/fake/chrome")
        with popen_patch as popen, binary_patch:
            call_command("talkroute_login", stdout=out)
        return popen, process, out.getvalue()

    def test_opens_the_saved_profile_in_a_plain_chrome_and_waits_until_it_is_closed(self):
        popen, process, output = self._run([subprocess.TimeoutExpired("chrome", 3), 0])
        command = popen.call_args[0][0]
        self.assertEqual(command[0], "/fake/chrome")
        self.assertIn("--user-data-dir=/tmp/tgpf-test-profiles/talkroute", command)
        self.assertEqual(command[-1], "https://my.talkroute.com/home/text_messages/")
        # a person has to be able to pass Cloudflare's check, so nothing may mark it as automated
        self.assertFalse([a for a in command if "remote-debugging" in a or "enable-automation" in a])
        self.assertEqual(process.wait.call_count, 2)
        self.assertIn("CLOSE THE WINDOW", output)
        self.assertIn("Saved", output)

    def test_a_chrome_that_exits_straight_away_gives_a_clear_error(self):
        with self.assertRaisesMessage(CommandError, "only be used by one Chrome"):
            self._run([0])
