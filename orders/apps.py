# imports from std lib
import os
import sys

# imports from django
from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orders'

    def ready(self):
        # Django dev server calls ready() twice: once in the reloader and once
        # in the actual server process. RUN_MAIN='true' only in the server process,
        # so we skip the reloader pass to avoid double-starting ngrok and
        # double-loading drivers into the in-memory state lists. With --noreload
        # there's no reloader fork at all, so RUN_MAIN is never set - in that
        # case we must not skip, since this is the only ready() call we get.
        if 'runserver' not in sys.argv:
            return
        if '--noreload' not in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        self._start_tunnel_and_register_webhooks()
        self._reset_stale_driver_locations()

    # opens the tunnel and registers its URLs in WebJoint. A failure no longer
    # takes the server down: it is logged and shown as a dashboard notice, because
    # without registered webhooks no new order will arrive.
    def _start_tunnel_and_register_webhooks(self):
        import logging
        from orders.services.notices import add_notice
        logger = logging.getLogger(__name__)
        try:
            from orders.management.commands.start_ngrok import start_ngrok
            url = start_ngrok()
        except Exception as e:
            logger.error("Webhook registration failed: %s", e)
            add_notice(f"Webhook registration FAILED - WebJoint will not send new orders: {e}", "error")
        else:
            logger.info("Webhooks registered in WebJoint at %s", url)
            add_notice(f"Webhooks registered in WebJoint at {url}")

    # nothing is held in memory any more: the only startup job is moving drivers
    # whose last known city is from an earlier day back to Davis
    def _reset_stale_driver_locations(self):
        try:
            from orders.services.startup import reset_stale_driver_locations
            reset_stale_driver_locations()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to reset driver locations on startup: %s", e)
