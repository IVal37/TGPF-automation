from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import Http404
from django.test import RequestFactory, TestCase

from orders.models import Driver, Order
from orders.services import dashboard, restock
from orders.services.engine import add_order, complete_order
from orders.services.eta import get_eta
from orders.tests.helpers import DbTruthCase, as_dispatcher


class TestPendingOrdersFsaFsm(TestCase):
    def test_linked_companion_disappears_from_queue_and_primary_shows_it(self):
        Order.objects.create(
            order_id="p1", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        primary = Order.objects.get(order_id="p1")
        Order.objects.create(
            order_id="p2", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
            linked_order=primary, status=Order.Status.SENT,
        )
        result = dashboard.pending_cards()
        order_ids = [item["order"].order_id for item in result]
        self.assertIn("p1", order_ids)
        self.assertNotIn("p2", order_ids)  # absorbed: a linked companion has status "sent", so it is not in the queue

        primary_item = next(item for item in result if item["order"].order_id == "p1")
        self.assertEqual(primary_item["linked_companion"].order_id, "p2")

    def test_fsa_fsm_card_shows_link_targets_excluding_itself_and_other_fsa(self):
        Order.objects.create(
            order_id="p3", customer_name="Jane Doe", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        Order.objects.create(
            order_id="p4", customer_name="Frank Sinatra Adult", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        Order.objects.create(
            order_id="p5", customer_name="Frank Sinatra Medical", city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        result = dashboard.pending_cards()
        fsa_item = next(item for item in result if item["order"].order_id == "p4")
        self.assertTrue(fsa_item["is_fsa_fsm"])
        target_ids = [t.order_id for t in fsa_item["link_targets"]]
        self.assertEqual(target_ids, ["p3"])  # excludes itself and the other FSA/FSM order


class TestPendingOrdersCard(TestCase):
    def _html(self, payment_type):
        from orders.views import pending_orders_partial
        driver = Driver.objects.create(name="D1", is_active=True)
        Order.objects.create(
            order_id="9001", driver=driver, customer_name="Jane Doe", city="Davis", payment_type=payment_type,
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0), eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        request = as_dispatcher(RequestFactory().get("/pending_orders_partial"))
        return pending_orders_partial(request).content.decode()

    def test_empty_payment_type_shows_checking_disables_send_and_offers_cash(self):
        html = self._html("")
        self.assertIn("Payment: checking", html)
        self.assertIn("disabled", html)
        self.assertIn('name="as_cash"', html)

    def test_resolved_payment_type_is_shown_and_send_is_enabled(self):
        html = self._html("Debit / Tap-to-pay")
        self.assertIn("Payment: Debit / Tap-to-pay", html)
        self.assertNotIn("checking", html)
        self.assertNotIn("disabled", html)
        self.assertNotIn("as_cash", html)


class TestUnassignedQueue(DbTruthCase):
    def _html(self):
        from orders.views import pending_orders_partial
        return pending_orders_partial(as_dispatcher(RequestFactory().get("/p"))).content.decode()

    def test_unassigned_card_has_a_driver_select_and_no_send(self):
        add_order(order_city="Davis", order_id="u1", customer_name="Pat Q", order_time=self.BASE)
        self._driver("Dee")
        html = self._html()
        self.assertIn("UNASSIGNED", html)
        self.assertIn('action="/assign_driver"', html)
        self.assertIn("Dee", html)
        self.assertNotIn('action="/send_dispatch_msg"', html)
        self.assertIn('name="csrfmiddlewaretoken"', html)

    def test_without_an_active_driver_the_card_says_to_switch_one_on(self):
        add_order(order_city="Davis", order_id="u1", customer_name="Pat Q", order_time=self.BASE)
        html = self._html()
        self.assertIn("Switch a driver on", html)
        self.assertNotIn('action="/assign_driver"', html)

    def test_already_texted_orders_are_marked(self):
        add_order(order_city="Davis", order_id="u1", customer_name="Pat Q", order_time=self.BASE)
        Order.objects.filter(order_id="u1").update(status=Order.Status.SENT)
        self.assertIn("Already texted", self._html())

    def test_unassigned_cards_come_first_and_an_assigned_card_is_normal(self):
        self._driver("Dee")
        self._add("assigned", "Davis"); get_eta("assigned")
        Order.objects.filter(order_id="assigned").update(customer_name="Assigned Cust")
        Order.objects.create(order_id="loose", customer_name="Loose Cust", city="Davis", order_date=self.BASE)
        html = self._html()
        self.assertLess(html.index("Loose Cust"), html.index("Assigned Cust"))
        self.assertEqual(html.count('action="/send_dispatch_msg"'), 1)

    def test_yesterdays_unassigned_order_is_not_shown(self):
        add_order(order_city="Davis", order_id="old", customer_name="Old Cust",
                  order_time=self.BASE - timedelta(days=1))
        self.assertNotIn("Old Cust", self._html())

    def test_send_is_refused_for_an_unassigned_order(self):
        from orders.views import send_dispatch_msg_view
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        Order.objects.filter(order_id="u1").update(payment_type="Cash")
        with patch("orders.tasks.send_message_task") as send_task, self.assertLogs("orders.views", "WARNING"):
            send_dispatch_msg_view(as_dispatcher(RequestFactory().post("/x", {"order_id": "u1"})))
        send_task.delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="u1").status, Order.Status.AWAITING_APPROVAL)


class TestDriverControlViews(DbTruthCase):
    def _post(self, view, **data):
        return view(as_dispatcher(RequestFactory().post("/x", data)))

    def test_add_driver_creates_an_on_shift_driver_starting_in_davis(self):
        from orders.views import add_driver_view
        self._post(add_driver_view, name="  Sam Rivera ")
        driver = Driver.objects.get(name="Sam Rivera")
        self.assertEqual((driver.is_active, driver.current_city), (True, "Davis"))

    def test_add_driver_ignores_a_blank_name(self):
        from orders.views import add_driver_view
        self._post(add_driver_view, name="   ")
        self.assertEqual(Driver.objects.count(), 0)

    def test_the_old_dummy_driver_view_is_gone(self):
        from orders import views
        self.assertFalse(hasattr(views, "add_dummy_driver"))

    def test_toggle_switches_a_driver_off_and_unassigns_their_orders(self):
        from orders.views import toggle_driver_view
        driver = self._driver("D1")
        self._add("o1", "Davis")
        self._post(toggle_driver_view, driver_id=driver.pk)
        self.assertFalse(Driver.objects.get(pk=driver.pk).is_active)
        self.assertIsNone(Order.objects.get(order_id="o1").driver_id)

    def test_toggle_switches_a_driver_back_on(self):
        from orders.views import toggle_driver_view
        driver = Driver.objects.create(name="D1", is_active=False)
        self._post(toggle_driver_view, driver_id=driver.pk)
        self.assertTrue(Driver.objects.get(pk=driver.pk).is_active)

    def test_toggle_unknown_driver_is_a_404(self):
        from orders.views import toggle_driver_view
        with self.assertRaises(Http404):
            self._post(toggle_driver_view, driver_id=999)

    def test_delete_driver_unassigns_their_orders(self):
        from orders.views import delete_driver_view
        driver = self._driver("D1")
        self._add("o1", "Davis")
        self._post(delete_driver_view, driver_id=driver.pk)
        self.assertFalse(Driver.objects.filter(pk=driver.pk).exists())
        self.assertIsNone(Order.objects.get(order_id="o1").driver_id)


class TestStartupPanel(DbTruthCase):
    def _page(self):
        from django.test import Client
        client = Client()
        client.force_login(User.objects.get_or_create(username="dispatcher")[0])
        return client.get("/").content.decode()

    def test_panel_has_add_driver_toggle_delete_with_confirm(self):
        driver = self._driver("Dee")
        html = self._page()
        self.assertIn('action="/add_driver"', html)
        self.assertIn('action="/toggle_driver"', html)
        self.assertIn('action="/delete_driver"', html)
        self.assertIn("confirm(", html)
        self.assertIn("Switch off", html)
        self.assertNotIn("add_dummy_driver", html)
        Driver.objects.filter(pk=driver.pk).update(is_active=False)
        self.assertIn("Switch on", self._page())

    def test_out_for_delivery_lists_each_drivers_sent_orders_with_mark_delivered(self):
        d1, d2 = self._driver("Dee"), self._driver("Eve")
        self._add("sent1", "Davis"); self._add("queued1", "Davis")
        Order.objects.filter(order_id="sent1").update(
            status=Order.Status.SENT, driver=d1, customer_name="Out Cust")
        Order.objects.filter(order_id="queued1").update(driver=d2, customer_name="Queued Cust")
        html = self._page()
        self.assertIn("Out Cust", html)
        self.assertEqual(html.count('action="/complete_order_manual"'), 1)
        self.assertIn('name="order_id" value="sent1"', html)
        self.assertNotIn("Queued Cust</span>", html)
        self.assertIn("Nothing out for delivery", html)  # Eve has none

    def test_mark_delivered_completes_the_order(self):
        from orders.views import complete_order_manual
        driver = self._driver("Dee")
        self._add("sent1", "Davis")
        Order.objects.filter(order_id="sent1").update(status=Order.Status.SENT)
        complete_order_manual(as_dispatcher(RequestFactory().post("/x", {"order_id": "sent1"})))
        self.assertEqual(Order.objects.get(order_id="sent1").status, Order.Status.COMPLETED)
        self.assertNotIn('name="order_id" value="sent1"', self._page())  # gone from out-for-delivery


class TestRestockTab(DbTruthCase):
    def _completed(self, order_id, items, **kw):
        self._add(order_id, "Davis")
        order = Order.objects.get(order_id=order_id)
        for name, qty in items:
            order.items.create(product_name=name, quantity=qty, price=Decimal("1"))
        complete_order(order_id)
        if kw:
            Order.objects.filter(order_id=order_id).update(**kw)

    def _post(self, view, **data):
        return view(as_dispatcher(RequestFactory().post("/x", data)))

    def test_full_restock_flow(self):
        from orders.views import reconcile_restock, clear_restock
        self._driver("D1")
        self._completed("a", [("Gummy", 2), ("Preroll", 1)])
        self._completed("b", [("Gummy", 3)])
        ctx = restock.restock_context()
        self.assertEqual([o.order_id for o in ctx["completed_orders"]], ["a", "b"])
        self.assertEqual(ctx["restock_items"], {})

        self._post(reconcile_restock, order_ids=["a"])
        ctx = restock.restock_context()
        self.assertEqual([o.order_id for o in ctx["completed_orders"]], ["b"])
        self.assertEqual(ctx["restock_items"], {"Gummy": {"quantity": 2}, "Preroll": {"quantity": 1}})

        self._post(reconcile_restock, order_ids=["b"])
        self.assertEqual(restock.restock_context()["restock_items"]["Gummy"], {"quantity": 5})

        self._post(clear_restock)
        ctx = restock.restock_context()
        self.assertEqual((ctx["completed_orders"], ctx["restock_items"]), ([], {}))
        self.assertTrue(Order.objects.get(order_id="a").restock_cleared)

    def test_cleared_orders_are_not_counted_again(self):
        from orders.views import reconcile_restock, clear_restock
        self._driver("D1")
        self._completed("a", [("Gummy", 2)])
        self._post(reconcile_restock, order_ids=["a"])
        self._post(clear_restock)
        self._completed("b", [("Gummy", 1)])
        self._post(reconcile_restock, order_ids=["b"])
        self.assertEqual(restock.restock_context()["restock_items"], {"Gummy": {"quantity": 1}})

    def test_only_completed_delivery_orders_are_listed(self):
        from orders.services.engine import add_shell_order, add_storefront_order, cancel_order
        self._driver("D1")
        self._completed("done", [("Gummy", 1)])
        self._add("cancelled", "Davis"); cancel_order("cancelled")
        self._add("open", "Davis")
        add_shell_order("Davis"); complete_order("noop", city="Davis")  # closes the shell
        add_storefront_order(order_id="store", customer_name="Walk In", order_time=self.BASE)
        self.assertEqual([o.order_id for o in restock.restock_context()["completed_orders"]], ["done"])

    def test_reconcile_ignores_orders_that_are_not_completed(self):
        from orders.views import reconcile_restock
        self._driver("D1")
        self._add("open", "Davis")
        self._post(reconcile_restock, order_ids=["open"])
        self.assertFalse(Order.objects.get(order_id="open").restock_reconciled)

    def test_restock_tab_renders_from_the_database(self):
        from orders.views import restock_partial
        self._driver("D1")
        self._completed("a", [("Gummy", 2)])
        html = restock_partial(as_dispatcher(RequestFactory().get("/restock_partial"))).content.decode()
        self.assertIn('value="a"', html)
        self.assertIn("No items yet", html)


class TestSetDriverLocationView(DbTruthCase):
    def test_sets_city_and_timestamp(self):
        from orders.views import set_driver_location_view
        driver = self._driver("D1")
        set_driver_location_view(as_dispatcher(RequestFactory().post("/x", {"driver_id": driver.pk, "city": "Winters"})))
        driver.refresh_from_db()
        self.assertEqual((driver.current_city, driver.location_updated_at), ("Winters", self.BASE))

    def test_inactive_driver_is_a_404_not_a_server_error(self):
        from orders.views import set_driver_location_view
        driver = Driver.objects.create(name="Off", is_active=False)
        with self.assertRaises(Http404):
            set_driver_location_view(as_dispatcher(RequestFactory().post("/x", {"driver_id": driver.pk, "city": "Winters"})))
