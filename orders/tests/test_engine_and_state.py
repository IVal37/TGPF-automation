from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase

from orders.models import Driver, Order
from orders.services import dashboard, queries, restock
from orders.services.engine import add_driver, add_order, complete_order
from orders.services.eta import get_eta
from orders.services.queries import get_driver_city_queues
from orders.tests.helpers import DbTruthCase, as_dispatcher


class TestEngineWritesStatus(TestCase):
    def setUp(self):
        self.driver = Driver.objects.create(name="D1", current_city="Davis")
        add_driver(self.driver.pk)

    def _add(self, order_id):
        add_order(order_city="Davis", order_id=order_id, customer_name="C", order_time=datetime.now())

    def test_new_order_awaits_approval(self):
        self._add("a1")
        order = Order.objects.get(order_id="a1")
        self.assertEqual(order.status, Order.Status.AWAITING_APPROVAL)
        self.assertFalse(order.is_shell)

    def test_complete_sets_completed(self):
        self._add("a1")
        complete_order("a1")
        order = Order.objects.get(order_id="a1")
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertIsNone(order.driver)

    def test_cancel_sets_cancelled(self):
        from orders.services.engine import cancel_order

        self._add("a1")
        cancel_order("a1")
        order = Order.objects.get(order_id="a1")
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertNotIn("a1", [o.order_id for o in queries.open_orders()])

    def test_shell_is_created_sent_and_closed_completed(self):
        from orders.services.engine import add_shell_order

        add_shell_order("Davis")
        shell = Order.objects.get(is_shell=True)
        self.assertTrue(shell.order_id.startswith("shell_"))
        self.assertEqual(shell.status, Order.Status.SENT)
        self._add("a1")
        complete_order("a1")  # completing an order in the shell's city closes the shell
        shell.refresh_from_db()
        self.assertEqual(shell.status, Order.Status.COMPLETED)

    def test_storefront_order_is_storefront(self):
        from orders.services.engine import add_storefront_order

        add_storefront_order(order_id="s1", customer_name="Walk In", order_time=datetime.now())
        order = Order.objects.get(order_id="s1")
        self.assertEqual(order.status, Order.Status.STOREFRONT)


class TestNoInMemoryState(SimpleTestCase):
    def test_the_state_module_is_gone(self):
        import importlib

        with self.assertRaises(ImportError):
            importlib.import_module("orders.services.state")


class TestRestartKeepsEverything(DbTruthCase):
    """Simulates a restart: re-runs the startup loader and checks nothing was lost."""

    def _snapshot(self):
        return {
            "drivers": {d.pk: d.current_city for d in queries.active_drivers()},
            "open": [(o.order_id, o.driver_id, o.eta_start, o.eta_end) for o in queries.open_orders()],
            "counts": queries.open_order_counts(),
            "real": queries.open_real_order_count(),
            "queues": {d.pk: dict(get_driver_city_queues(d.pk)) for d in queries.active_drivers()},
            "pick": [self._pick(c) for c in ("Davis", "Winters", "Fairfield")],
            "pending": [(i["order"].order_id, i["order"].status) for i in dashboard.pending_cards()],
            "restock": restock.restock_context(),
        }

    def _pick(self, city):
        from orders.services.engine import _pick_driver

        return _pick_driver(city).pk

    def _build(self):
        self.d1 = self._driver("D1")
        self.d2 = self._driver("D2")
        self._add("o1", "Winters")
        self._add("o2", "Davis")
        self._add("o3", "Fairfield")
        from orders.services.engine import add_shell_order

        add_shell_order("Davis")
        for oid in ("o1", "o2", "o3"):
            get_eta(oid)
        Order.objects.filter(order_id="o2").update(status=Order.Status.SENT)
        Order.objects.filter(order_id="o3").update(payment_type="", status=Order.Status.AWAITING_PAYMENT)
        # two finished orders, one already reconciled...
        self._add("done1", "Davis")
        self._add("done2", "Davis")
        for oid, qty in (("done1", 2), ("done2", 3)):
            Order.objects.get(order_id=oid).items.create(product_name="Gummy", quantity=qty, price=Decimal("1"))
            complete_order(oid)
        Order.objects.filter(order_id="done1").update(restock_reconciled=True)
        # ...and a driver who has moved on today
        Driver.objects.get(pk=self.d1.pk).set_current_city("Fairfield")

    def test_restart_changes_nothing_when_drivers_moved_today(self):
        from orders.services.startup import reset_stale_driver_locations

        self._build()
        before = self._snapshot()
        reset_stale_driver_locations()
        after = self._snapshot()
        self.assertEqual(before, after)
        self.assertEqual(Driver.objects.get(pk=self.d1.pk).current_city, "Fairfield")
        # the restock tab and list survive too
        self.assertEqual([o.order_id for o in after["restock"]["completed_orders"]], ["done2"])
        self.assertEqual(after["restock"]["restock_items"], {"Gummy": {"quantity": 2}})

    def test_restart_resets_a_city_set_on_an_earlier_day(self):
        from orders.services.startup import reset_stale_driver_locations

        self._build()
        Driver.objects.filter(pk=self.d1.pk).update(location_updated_at=self.BASE - timedelta(days=1))
        reset_stale_driver_locations()
        driver = Driver.objects.get(pk=self.d1.pk)
        self.assertEqual(driver.current_city, "Davis")
        self.assertEqual(driver.location_updated_at, self.BASE)
        # the other driver, set today, is untouched
        Driver.objects.filter(pk=self.d2.pk).update(current_city="Winters", location_updated_at=self.BASE)
        reset_stale_driver_locations()
        self.assertEqual(Driver.objects.get(pk=self.d2.pk).current_city, "Winters")

    def test_restart_resets_a_city_that_was_never_timestamped(self):
        from orders.services.startup import reset_stale_driver_locations

        driver = Driver.objects.create(name="Old", current_city="Winters", is_active=True)
        reset_stale_driver_locations()
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Davis")

    def test_ready_resets_through_the_apps_hook(self):
        from orders.apps import OrdersConfig

        driver = Driver.objects.create(name="Old", current_city="Winters", is_active=True)
        OrdersConfig("orders", __import__("orders"))._reset_stale_driver_locations()
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Davis")


class TestOpenOrderQueries(DbTruthCase):
    def test_open_orders_are_todays_open_orders_with_a_driver(self):
        self._driver("D1")
        self._add("today", "Davis")
        self._add("yesterday", "Davis", when=self.BASE - timedelta(days=1))
        self._add("closed", "Davis")
        complete_order("closed")
        self._add("cancelled", "Davis")
        from orders.services.engine import cancel_order

        cancel_order("cancelled")
        self.assertEqual([o.order_id for o in queries.open_orders()], ["today"])

    def test_yesterdays_open_order_does_not_count_toward_load_route_or_window(self):
        driver = self._driver("D1")
        for i in range(8):
            self._add(f"old{i}", "Davis", when=self.BASE - timedelta(days=1))
        self._add("o1", "Davis")
        self.assertEqual(queries.open_order_counts(), {driver.pk: 1})
        self.assertEqual(dict(get_driver_city_queues(driver.pk)), {"Davis": ["o1"]})
        get_eta("o1")
        order = Order.objects.get(order_id="o1")
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_open_shells_count_toward_load_but_not_the_window_count(self):
        from orders.services.engine import add_shell_order

        driver = self._driver("D1")
        self._add("o1", "Davis")
        add_shell_order("Davis")
        self.assertEqual(queries.open_order_counts(), {driver.pk: 2})
        self.assertEqual(queries.open_real_order_count(), 1)

    def test_pick_driver_prefers_the_less_loaded_driver(self):
        from orders.services.engine import _pick_driver

        busy, free = self._driver("Busy"), self._driver("Free")
        self._add("o1", "Davis")  # goes to Busy (lowest id on a tie)
        self.assertEqual(Order.objects.get(order_id="o1").driver_id, busy.pk)
        self.assertEqual(_pick_driver("Davis").pk, free.pk)

    def test_route_order_is_the_order_orders_were_added(self):
        driver = self._driver("D1")
        for oid in ("a", "b", "c"):
            self._add(oid, "Davis")
        self.assertEqual(get_driver_city_queues(driver.pk)["Davis"], ["a", "b", "c"])

    def test_a_repeat_complete_webhook_does_not_move_a_driver_or_close_another_shell(self):
        from orders.services.engine import add_shell_order

        driver = self._driver("D1")
        self._add("o1", "Fairfield")
        add_shell_order("Fairfield")
        add_shell_order("Fairfield")
        complete_order("o1", city="Fairfield")  # closes o1 and one shell
        self.assertEqual(Order.objects.filter(is_shell=True, status=Order.Status.COMPLETED).count(), 1)
        Driver.objects.get(pk=driver.pk).set_current_city("Davis")
        complete_order("o1", city="Fairfield")  # same webhook again
        self.assertEqual(Order.objects.filter(is_shell=True, status=Order.Status.COMPLETED).count(), 1)
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Davis")

    def test_completing_an_order_from_before_today_closes_a_matching_shell(self):
        from orders.services.engine import add_shell_order

        driver = self._driver("D1")
        add_shell_order("Fairfield")
        self._add("old", "Fairfield", when=self.BASE - timedelta(days=1))
        complete_order("old", city="Fairfield")
        self.assertEqual(Order.objects.get(is_shell=True).status, Order.Status.COMPLETED)
        self.assertEqual(Driver.objects.get(pk=driver.pk).current_city, "Fairfield")


class TestNoStaleSaves(DbTruthCase):
    """A save from an out-of-date instance must not undo fields changed with .update()."""

    def test_set_etas_leaves_other_fields_alone(self):
        self._driver("D1")
        self._add("o1", "Davis")
        stale = Order.objects.get(order_id="o1")
        Order.objects.filter(order_id="o1").update(payment_type="Debit", status=Order.Status.SENT)
        stale.set_etas(self.BASE, self.BASE + timedelta(hours=1))
        fresh = Order.objects.get(order_id="o1")
        self.assertEqual((fresh.payment_type, fresh.status), ("Debit", Order.Status.SENT))
        self.assertEqual(fresh.eta_end, self.BASE + timedelta(hours=1))

    def test_driver_add_order_leaves_other_fields_alone(self):
        driver = self._driver("D1")
        Order.objects.create(order_id="x", city="Davis", order_date=self.BASE)
        stale = Order.objects.get(order_id="x")
        Order.objects.filter(order_id="x").update(payment_type="Cash", status=Order.Status.SENT)
        driver.add_order(stale)
        fresh = Order.objects.get(order_id="x")
        self.assertEqual((fresh.payment_type, fresh.status, fresh.driver_id), ("Cash", Order.Status.SENT, driver.pk))

    def test_set_current_city_leaves_other_fields_alone(self):
        driver = self._driver("D1")
        stale = Driver.objects.get(pk=driver.pk)
        Driver.objects.filter(pk=driver.pk).update(is_active=False)
        stale.set_current_city("Winters")
        fresh = Driver.objects.get(pk=driver.pk)
        self.assertFalse(fresh.is_active)
        self.assertEqual((fresh.current_city, fresh.location_updated_at), ("Winters", self.BASE))


class TestAddOrderWithoutADriver(DbTruthCase):
    def test_order_is_saved_unassigned_instead_of_raising(self):
        with self.assertLogs("orders.services.engine", "WARNING"):
            order = add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        saved = Order.objects.get(order_id="u1")
        self.assertIsNone(saved.driver_id)
        self.assertIsNone(saved.eta_start)
        self.assertEqual(order.pk, saved.pk)
        self.assertEqual([o.order_id for o in queries.unassigned_orders()], ["u1"])

    def test_scheduled_window_is_stored(self):
        start, end = self.BASE + timedelta(hours=3), self.BASE + timedelta(hours=4)
        add_order(
            order_city="Davis",
            order_id="u1",
            customer_name="C",
            order_time=self.BASE,
            scheduled_start=start,
            scheduled_end=end,
        )
        saved = Order.objects.get(order_id="u1")
        self.assertEqual((saved.scheduled_start, saved.scheduled_end), (start, end))

    def test_a_shell_still_needs_a_driver(self):
        from orders.services.engine import add_shell_order

        with self.assertRaises(ValueError):
            add_shell_order("Davis")

    def test_shell_view_does_not_crash_without_a_driver(self):
        from orders.views import add_shell_order_view

        with self.assertLogs("orders.views", "WARNING"):
            response = add_shell_order_view(as_dispatcher(RequestFactory().post("/x", {"city": "Davis"})))
        self.assertEqual(response.status_code, 302)


class TestAssignOrder(DbTruthCase):
    def setUp(self):
        super().setUp()
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        self.driver = self._driver("D1")

    def test_queued_order_gets_a_driver_and_an_eta(self):
        from orders.services.engine import assign_order

        assign_order("u1", self.driver.pk)
        order = Order.objects.get(order_id="u1")
        self.assertEqual(order.driver_id, self.driver.pk)
        self.assertEqual(order.eta_start, self.BASE + timedelta(minutes=30))
        self.assertEqual(order.eta_end - order.eta_start, timedelta(minutes=60))

    def test_scheduled_window_wins_when_it_is_no_earlier_than_the_estimate(self):
        from orders.services.engine import assign_order

        start, end = self.BASE + timedelta(hours=3), self.BASE + timedelta(hours=4)
        Order.objects.filter(order_id="u1").update(scheduled_start=start, scheduled_end=end)
        assign_order("u1", self.driver.pk)
        order = Order.objects.get(order_id="u1")
        self.assertEqual((order.eta_start, order.eta_end), (start, end))

    def test_scheduled_window_is_ignored_when_the_estimate_is_later(self):
        from orders.services.engine import assign_order

        start, end = self.BASE, self.BASE + timedelta(minutes=10)
        Order.objects.filter(order_id="u1").update(scheduled_start=start, scheduled_end=end)
        assign_order("u1", self.driver.pk)
        self.assertEqual(Order.objects.get(order_id="u1").eta_start, self.BASE + timedelta(minutes=30))

    def test_an_already_texted_order_keeps_its_eta(self):
        from orders.services.engine import assign_order

        told = (self.BASE + timedelta(hours=1), self.BASE + timedelta(hours=2))
        Order.objects.filter(order_id="u1").update(status=Order.Status.SENT, eta_start=told[0], eta_end=told[1])
        assign_order("u1", self.driver.pk)
        order = Order.objects.get(order_id="u1")
        self.assertEqual((order.driver_id, order.eta_start, order.eta_end), (self.driver.pk, told[0], told[1]))

    def test_inactive_driver_cannot_be_assigned(self):
        from orders.services.engine import assign_order

        Driver.objects.filter(pk=self.driver.pk).update(is_active=False)
        with self.assertRaises(Driver.DoesNotExist):
            assign_order("u1", self.driver.pk)


class TestRemoveAndDeleteDriver(DbTruthCase):
    def setUp(self):
        super().setUp()
        from orders.services.engine import add_shell_order

        self.d1, self.d2 = self._driver("D1"), self._driver("D2")
        self._add("queued", "Davis")  # -> D1
        self._add("other", "Davis")  # -> D2
        self._add("out", "Davis")
        Order.objects.filter(order_id="out").update(status=Order.Status.SENT, driver=self.d1)
        self._add("done", "Davis")
        complete_order("done")
        Order.objects.filter(order_id="queued").update(driver=self.d1)
        add_shell_order("Davis")
        Order.objects.filter(is_shell=True).update(driver=self.d1)

    def test_remove_driver_unassigns_open_orders_including_sent_ones(self):
        from orders.services.engine import remove_driver

        remove_driver(self.d1.pk)
        self.assertFalse(Driver.objects.get(pk=self.d1.pk).is_active)
        for oid in ("queued", "out"):
            self.assertIsNone(Order.objects.get(order_id=oid).driver_id, oid)
        self.assertEqual(Order.objects.get(order_id="out").status, Order.Status.SENT)
        self.assertEqual(Order.objects.get(order_id="queued").status, Order.Status.AWAITING_APPROVAL)
        self.assertEqual(sorted(o.order_id for o in queries.unassigned_orders()), ["out", "queued"])

    def test_remove_driver_closes_their_shells_and_leaves_others_alone(self):
        from orders.services.engine import remove_driver

        remove_driver(self.d1.pk)
        shell = Order.objects.get(is_shell=True)
        self.assertEqual((shell.status, shell.driver_id), (Order.Status.COMPLETED, None))
        self.assertEqual(Order.objects.get(order_id="other").driver_id, self.d2.pk)
        self.assertEqual(Order.objects.get(order_id="done").status, Order.Status.COMPLETED)

    def test_delete_driver_unassigns_then_deletes(self):
        from orders.services.engine import delete_driver

        delete_driver(self.d1.pk)
        self.assertFalse(Driver.objects.filter(pk=self.d1.pk).exists())
        self.assertIsNone(Order.objects.get(order_id="queued").driver_id)
        self.assertEqual(Order.objects.get(order_id="queued").status, Order.Status.AWAITING_APPROVAL)


class TestPhase5bSecurity(TestCase):
    NEW_VIEWS = ("/add_driver", "/toggle_driver", "/assign_driver")

    def test_anonymous_posts_redirect_to_login(self):
        from django.test import Client

        for path in self.NEW_VIEWS:
            with self.subTest(path=path):
                response = Client().post(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_posts_without_a_csrf_token_are_forbidden(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_user("dispatcher"))
        for path in self.NEW_VIEWS:
            with self.subTest(path=path):
                self.assertEqual(client.post(path).status_code, 403)

    def test_get_is_not_allowed(self):
        from django.test import Client

        client = Client()
        client.force_login(User.objects.create_user("dispatcher"))
        for path in self.NEW_VIEWS:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 405)
