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
        from orders.management.commands.start_ngrok import start_ngrok
        start_ngrok()
        self._reset_stale_driver_locations()

    # nothing is held in memory any more: the only startup job is moving drivers
    # whose last known city is from an earlier day back to Davis
    def _reset_stale_driver_locations(self):
        try:
            from orders.services.startup import reset_stale_driver_locations
            reset_stale_driver_locations()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to reset driver locations on startup: %s", e)
