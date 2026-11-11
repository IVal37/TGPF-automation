# imports from std lib
import os
import sys

# imports from django
from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orders'

    def ready(self) -> None:
        # Django dev server calls ready() twice: once in the reloader and once
        # in the actual server process. RUN_MAIN='true' only in the server process,
        # so we skip the reloader pass to avoid starting a second ngrok tunnel.
        # With --noreload there's no reloader fork at all, so RUN_MAIN is never
        # set - in that case we must not skip, since this is the only ready()
        # call we get.
        if 'runserver' not in sys.argv:
            return
        if '--noreload' not in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        self._start_tunnel_and_register_webhooks()
        self._reset_stale_driver_locations()

    # Opens the tunnel and registers its URLs in WebJoint. A failure no longer
    # takes the server down: it is logged and shown as a dashboard notice, because
    # without registered webhooks no new order will arrive.
    #
    # This is a WebJoint write that RESTOCK_TEST_MODE deliberately does NOT block:
    # test mode still needs real orders to arrive. It only changes the webhook
    # addresses, never an order or a customer (the exception is also in CLAUDE.md).
    def _start_tunnel_and_register_webhooks(self) -> None:
        import logging

        from orders.services.notices import add_notice
        logger = logging.getLogger(__name__)
        try:
            from orders.management.commands.start_ngrok import start_ngrok
            url = start_ngrok()
        except Exception as e:  # deliberate: any failure here must not stop the server
            logger.exception("Webhook registration failed: %s", e)
            add_notice(f"Webhook registration FAILED - WebJoint will not send new orders: {e}", "error")
        else:
            logger.info("Webhooks registered in WebJoint at %s", url)
            add_notice(f"Webhooks registered in WebJoint at {url}")

    # the only startup job: moving drivers whose last known city is from an
    # earlier day back to the home city
    def _reset_stale_driver_locations(self) -> None:
        try:
            from orders.services.startup import reset_stale_driver_locations
            reset_stale_driver_locations()
        except Exception:  # deliberate: a database hiccup at startup must not stop the server
            import logging
            logging.getLogger(__name__).exception("Failed to reset driver locations on startup")
