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
        # double-loading drivers into the in-memory state lists.
        if os.environ.get('RUN_MAIN') != 'true':
            return
        if 'runserver' in sys.argv:
            from orders.management.commands.start_ngrok import start_ngrok
            start_ngrok()
        self._load_active_drivers()

    def _load_active_drivers(self):
        try:
            from orders.models import Driver
            from orders.services.state import active_drivers, drivers_by_id
            for driver in Driver.objects.filter(is_active=True):
                active_drivers.append(driver)
                drivers_by_id[driver.get_id()] = driver
        except Exception:
            pass
