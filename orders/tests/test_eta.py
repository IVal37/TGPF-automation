from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase

from orders.models import Driver, Order
from orders.services.engine import add_driver, add_order, complete_order
from orders.services.eta import get_eta


class TestGetEta(TestCase):
    # Monday 2pm: well clear of the 10:30am opening clamp and the 10pm/5pm closing times,
    # so none of the business-hours logic interferes with the math below.
    BASE = datetime(2026, 7, 20, 14, 0, 0)

    def setUp(self):
        # "today" is the fixture day, so the open-order queries and shell dates line up
        patcher = patch("django.utils.timezone.now", return_value=self.BASE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.driver = Driver.objects.create(name="Test Driver", current_city="Davis")
        add_driver(self.driver.pk)

    def _add(self, order_id, city, when=None):
        add_order(order_city=city, order_id=order_id, customer_name="Test", order_time=when or self.BASE)

    def test_single_order_gets_30min_floor_and_60min_window(self):
        # Davis -> Davis is same-city travel (15) + delivery (5) = 20 raw minutes,
        # below the 30-minute floor, so the floor should win.
        self._add("o1", "Davis")
        get_eta("o1")
        order = Order.objects.get(order_id="o1")
        self.assertEqual(order.eta_start, self.BASE + timedelta(minutes=30))
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_etas_never_go_backwards_down_the_route(self):
        # Regression test for a bug where the sliding anchor (which pulls a later
        # stop's window start earlier, toward the window's middle) was sized off
        # the window length instead of the real travel time between stops. With
        # two orders in the same city, the old formula pulled the second order's
        # window a full 30 minutes earlier regardless of the real ~20-minute gap
        # between the stops, inverting the two windows (confirmed against the
        # pre-fix code: o2 landed at 14:30, 15 min *before* o1's 14:45).
        #
        # Driver starts in Davis. o1 is the first stop, in Winters
        # (Davis->Winters = 30+5 = 35 raw min). o2 is the second stop, same
        # city as o1 (+15+5 = 20 more, cumulative 55).
        self._add("o1", "Winters")
        self._add("o2", "Winters")

        get_eta("o1")
        get_eta("o2")

        o1, o2 = Order.objects.get(order_id="o1"), Order.objects.get(order_id="o2")
        self.assertLessEqual(o1.eta_start, o2.eta_start)
        for o in (o1, o2):
            self.assertEqual(o.eta_end - o.eta_start, timedelta(minutes=60))

    def test_window_widens_past_five_real_orders(self):
        for i in range(6):
            self._add(f"o{i}", "Davis")
        get_eta("o0")
        order = Order.objects.get(order_id="o0")
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=90))

    def test_shell_orders_dont_count_toward_window_widening(self):
        from orders.services.engine import add_shell_order
        self._add("o1", "Davis")
        for _ in range(6):
            add_shell_order("Davis")
        get_eta("o1")
        order = Order.objects.get(order_id="o1")
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_completing_an_order_updates_driver_location_for_next_eta(self):
        self._add("o1", "Fairfield")
        get_eta("o1")
        complete_order("o1")
        live_driver = Driver.objects.get(pk=self.driver.pk)
        self.assertEqual(live_driver.current_city, "Fairfield")

        # next order placed in the city the driver just moved to should only need
        # same-city travel + delivery (15 + 5 = 20 raw minutes), still under the floor.
        self._add("o2", "Fairfield")
        get_eta("o2")
        order2 = Order.objects.get(order_id="o2")
        self.assertEqual(order2.eta_start, self.BASE + timedelta(minutes=30))
