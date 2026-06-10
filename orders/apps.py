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
            from orders.models import Driver, Order
            from orders.services.state import active_drivers, drivers_by_id, orders_by_id, driver_id_by_order_id
            from django.utils import timezone
            for driver in Driver.objects.filter(is_active=True):
                active_drivers.append(driver)
                drivers_by_id[driver.get_id()] = driver
            today = timezone.now().date()
            for driver in active_drivers:
                driver.current_city = "Davis"
                driver.save()
            today_orders = list(Order.objects.filter(completed=False, driver__isnull=False, order_date__date=today).select_related('driver'))
            for order in today_orders:
                orders_by_id[order.order_id] = order
                driver_id_by_order_id[order.order_id] = order.driver_id
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("Failed to load state from DB on startup: %s", e)
