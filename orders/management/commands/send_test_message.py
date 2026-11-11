# imports from django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Send a harmless 'testing' message to check a channel works: --sms texts "
        "TALKROUTE_TEST_PHONE through TalkRoute, --telegram posts to the restock channel."
    )

    def add_arguments(self, parser) -> None:
        channel = parser.add_mutually_exclusive_group(required=True)
        channel.add_argument("--sms", action="store_true", help="text TALKROUTE_TEST_PHONE")
        channel.add_argument("--telegram", action="store_true", help="post to the Telegram channel")

    def handle(self, *args, **options) -> None:
        if settings.RESTOCK_TEST_MODE:
            raise CommandError("RESTOCK_TEST_MODE is on: this command really sends, so it will not run.")

        if options["sms"]:
            if not settings.TALKROUTE_TEST_PHONE:
                raise CommandError("TALKROUTE_TEST_PHONE is not set in .env")
            from orders.services.scrapers.talkroute import send_message
            send_message(settings.TALKROUTE_TEST_PHONE, "testing", order_ref="test")
        else:
            from orders.services.telegram import send_telegram_message
            send_telegram_message("testing")
        self.stdout.write("Sent.")
