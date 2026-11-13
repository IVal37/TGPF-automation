from datetime import datetime
from decimal import Decimal
from unittest.mock import ANY, patch

from django.http import Http404
from django.test import RequestFactory, TestCase

from orders.models import Driver, Order
from orders.services import dashboard, queries
from orders.services.engine import add_order
from orders.tests.helpers import WJ_ID, DbTruthCase, as_dispatcher


class TestSendDispatchMsgView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.driver = Driver.objects.create(name="D1", is_active=True)
        self.order = Order.objects.create(
            order_id="9001",
            driver=self.driver,
            customer_name="Jane Doe",
            customer_phone="15305551234",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
            payment_type="Cash",
            total=Decimal("50.00"),
            discount=Decimal("0.00"),
            webjoint_id="1111-2222-3333333-9001",
        )
        # a newer order that arrived since: notes must still go to order 9001
        Order.objects.create(
            order_id="9099",
            customer_name="Newer Customer",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 20, 0),
            eta_start=datetime(2026, 7, 20, 15, 0, 0),
            eta_end=datetime(2026, 7, 20, 16, 0, 0),
            webjoint_id="1111-2222-4444444-9099",
        )

    def test_sends_message_with_edited_window_and_marks_the_order_sending(self):
        from orders.views import send_dispatch_msg_view

        with patch("orders.tasks.send_message_task") as mock_send_task, patch(
            "orders.tasks.fill_order_notes_task"
        ) as mock_notes_task:
            request = self.factory.post(
                "/send_dispatch_msg",
                {
                    "order_id": "9001",
                    "eta_start": "14:45",
                    "eta_end": "15:45",
                },
            )
            response = send_dispatch_msg_view(as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        mock_send_task.delay.assert_called_once()
        self.assertEqual(mock_send_task.delay.call_args.args[:2], ("9001", "15305551234"))
        self.assertIn("2:45-3:45", mock_send_task.delay.call_args.args[2])
        mock_notes_task.delay.assert_called_once()
        # the note targets this order's WebJoint id, not the newer order's
        self.assertEqual(mock_notes_task.delay.call_args.args[0], "1111-2222-3333333-9001")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.SENDING)
        self.assertEqual(self.order.eta_start, datetime(2026, 7, 20, 14, 45, 0))
        self.assertEqual(self.order.eta_end, datetime(2026, 7, 20, 15, 45, 0))

    def test_order_without_webjoint_id_is_texted_but_note_is_skipped(self):
        from orders.views import send_dispatch_msg_view

        Order.objects.filter(order_id="9001").update(webjoint_id="")
        with patch("orders.tasks.send_message_task") as mock_send_task, patch(
            "orders.tasks.fill_order_notes_task"
        ) as mock_notes_task, self.assertLogs("orders.services.dispatch_queue", "WARNING") as logs:
            request = self.factory.post(
                "/send_dispatch_msg",
                {
                    "order_id": "9001",
                    "eta_start": "14:45",
                    "eta_end": "15:45",
                },
            )
            send_dispatch_msg_view(as_dispatcher(request))

        mock_send_task.delay.assert_called_once()
        mock_notes_task.delay.assert_not_called()
        self.assertTrue(any("9001" in line for line in logs.output))

    def test_already_sent_order_is_not_resent(self):
        # guards against double-clicking Send and texting the customer twice
        from orders.views import send_dispatch_msg_view

        self.order.status = Order.Status.SENT
        self.order.save()
        request = self.factory.post(
            "/send_dispatch_msg",
            {
                "order_id": "9001",
                "eta_start": "14:30",
                "eta_end": "15:30",
            },
        )
        with self.assertRaises(Http404):
            send_dispatch_msg_view(as_dispatcher(request))


class TestSendRescheduleMsgView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        # Monday 10:30pm — past the 10pm weekday closing time
        self.order = Order.objects.create(
            order_id="9002",
            customer_name="John Smith",
            customer_phone="15305559999",
            city="Davis",
            order_date=datetime(2026, 7, 20, 21, 0, 0),
            eta_start=datetime(2026, 7, 20, 22, 30, 0),
            eta_end=datetime(2026, 7, 20, 23, 30, 0),
            webjoint_id="1111-2222-3333333-9002",
        )
        Order.objects.create(
            order_id="9098",
            customer_name="Newer Customer",
            city="Davis",
            order_date=datetime(2026, 7, 20, 22, 0, 0),
            eta_start=datetime(2026, 7, 20, 23, 0, 0),
            eta_end=datetime(2026, 7, 21, 0, 0, 0),
            webjoint_id="1111-2222-4444444-9098",
        )

    def test_sends_reschedule_and_marks_the_order_sending(self):
        from orders.views import send_reschedule_msg_view

        with patch("orders.tasks.send_message_task") as mock_send_task, patch(
            "orders.tasks.fill_order_notes_task"
        ) as mock_notes_task:
            request = self.factory.post("/send_reschedule_msg", {"order_id": "9002"})
            response = send_reschedule_msg_view(as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        mock_send_task.delay.assert_called_once_with(str(self.order.order_id), "15305559999", ANY)
        self.assertIn("reschedule", mock_send_task.delay.call_args.args[2].lower())
        mock_notes_task.delay.assert_called_once_with(
            "1111-2222-3333333-9002",
            "reached out about rescheduling for tomorrow morning",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.SENDING)


class TestLinkOrderView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.primary = Order.objects.create(
            order_id="l1",
            customer_name="Jane Doe",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
            total=Decimal("50.00"),
            delivery_fee=Decimal("5.00"),
            webjoint_id="1111-2222-3333333-1001",
        )
        self.companion = Order.objects.create(
            order_id="l2",
            customer_name="Frank Sinatra Adult",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
            total=Decimal("30.00"),
            delivery_fee=Decimal("5.00"),
            webjoint_id="1111-2222-5555555-1002",
        )

    def test_links_companion_and_writes_note(self):
        from orders.views import link_order_view

        with patch("orders.tasks.fill_order_notes_task") as mock_notes_task:
            request = self.factory.post(
                "/link_order",
                {
                    "order_id": "l2",
                    "primary_order_id": "l1",
                },
            )
            response = link_order_view(as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        self.companion.refresh_from_db()
        self.assertEqual(self.companion.linked_order_id, self.primary.pk)
        self.assertEqual(self.companion.status, Order.Status.SENT)
        # the note is written on the companion's WebJoint order, not the primary's
        mock_notes_task.delay.assert_called_once_with(
            "1111-2222-5555555-1002",
            "second part of Jane Doe's order",
        )

    def test_rejects_non_fsa_fsm_order(self):
        other = Order.objects.create(
            order_id="l3",
            customer_name="Some Rando",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        from orders.views import link_order_view

        request = self.factory.post(
            "/link_order",
            {
                "order_id": "l3",
                "primary_order_id": "l1",
            },
        )
        link_order_view(as_dispatcher(request))
        other.refresh_from_db()
        self.assertIsNone(other.linked_order)
        self.assertEqual(other.status, Order.Status.AWAITING_APPROVAL)

    def test_rejects_when_primary_already_sent(self):
        self.primary.status = Order.Status.SENT
        self.primary.save()
        from orders.views import link_order_view

        request = self.factory.post(
            "/link_order",
            {
                "order_id": "l2",
                "primary_order_id": "l1",
            },
        )
        with self.assertRaises(Http404):
            link_order_view(as_dispatcher(request))


class TestUnlinkOrderView(TestCase):
    def test_reverses_link(self):
        primary = Order.objects.create(
            order_id="u1",
            customer_name="Jane Doe",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
        )
        companion = Order.objects.create(
            order_id="u2",
            customer_name="Frank Sinatra Medical",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            linked_order=primary,
            status=Order.Status.SENT,
        )
        from orders.views import unlink_order_view

        factory = RequestFactory()
        request = factory.post("/unlink_order", {"order_id": "u2"})
        response = unlink_order_view(as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        companion.refresh_from_db()
        self.assertIsNone(companion.linked_order)
        self.assertEqual(companion.status, Order.Status.AWAITING_PAYMENT)  # it has no payment type


class TestClearDispatchQueueView(TestCase):
    def test_marks_all_pending_orders_sent_without_texting(self):
        Order.objects.create(
            order_id="c1",
            customer_name="Jane Doe",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        Order.objects.create(
            order_id="c2",
            customer_name="John Smith",
            city="Woodland",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
        )
        # under-min order (no eta) was never in the queue — should be untouched
        Order.objects.create(
            order_id="c3",
            customer_name="Under Min",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
        )

        from orders.views import clear_dispatch_queue_view

        factory = RequestFactory()
        with patch("orders.tasks.send_message_task") as mock_send_task, patch(
            "orders.tasks.fill_order_notes_task"
        ) as mock_notes_task:
            request = factory.post("/clear_dispatch_queue")
            response = clear_dispatch_queue_view(as_dispatcher(request))

        self.assertEqual(response.status_code, 302)
        mock_send_task.delay.assert_not_called()
        mock_notes_task.delay.assert_not_called()

        self.assertEqual(Order.objects.get(order_id="c1").status, Order.Status.SENT)
        self.assertEqual(Order.objects.get(order_id="c2").status, Order.Status.SENT)
        self.assertNotEqual(Order.objects.get(order_id="c3").status, Order.Status.SENT)


class TestSendDispatchPaymentGate(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.driver = Driver.objects.create(name="D1", is_active=True)
        self.order = Order.objects.create(
            order_id="9001",
            driver=self.driver,
            customer_name="Jane Doe",
            customer_phone="15305551234",
            city="Davis",
            source="External",
            payment_type="",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
            total=Decimal("50.00"),
            webjoint_id="1111-2222-3333333-9001",
        )

    def _send(self, **extra):
        from orders.views import send_dispatch_msg_view

        request = self.factory.post(
            "/send_dispatch_msg",
            {
                "order_id": "9001",
                "eta_start": "14:45",
                "eta_end": "15:45",
                **extra,
            },
        )
        with patch("orders.tasks.send_message_task") as send_task, patch(
            "orders.tasks.fill_order_notes_task"
        ) as notes_task:
            response = send_dispatch_msg_view(as_dispatcher(request))
        self.order.refresh_from_db()
        return response, send_task, notes_task

    def test_send_is_refused_while_payment_type_is_empty(self):
        with self.assertLogs("orders.views", "WARNING"):
            response, send_task, notes_task = self._send()
        self.assertEqual(response.status_code, 302)
        send_task.delay.assert_not_called()
        notes_task.delay.assert_not_called()
        self.assertEqual(self.order.status, Order.Status.AWAITING_APPROVAL)
        self.assertEqual(self.order.payment_type, "")
        # the ETA edit isn't applied either: nothing about a refused send sticks
        self.assertEqual(self.order.eta_start, datetime(2026, 7, 20, 14, 30, 0))

    def test_send_as_cash_sets_cash_and_sends(self):
        _, send_task, notes_task = self._send(as_cash="1")
        self.assertEqual(self.order.payment_type, "Cash")
        self.assertEqual(self.order.status, Order.Status.SENDING)
        send_task.delay.assert_called_once()
        self.assertEqual(notes_task.delay.call_args.args[0], "1111-2222-3333333-9001")
        self.assertIn("WM says cash", notes_task.delay.call_args.args[1])

    def test_send_as_cash_is_refused_once_the_real_type_has_resolved(self):
        Order.objects.filter(order_id="9001").update(payment_type="Debit / Tap-to-pay")
        with self.assertLogs("orders.views", "WARNING"):
            _, send_task, _ = self._send(as_cash="1")
        send_task.delay.assert_not_called()
        self.assertEqual(self.order.payment_type, "Debit / Tap-to-pay")
        self.assertEqual(self.order.status, Order.Status.AWAITING_APPROVAL)

    def test_plain_send_works_once_the_type_has_resolved(self):
        Order.objects.filter(order_id="9001").update(payment_type="Debit / Tap-to-pay")
        _, send_task, _ = self._send()
        send_task.delay.assert_called_once()
        self.assertEqual(self.order.status, Order.Status.SENDING)


class TestAssignDriverView(DbTruthCase):
    def _assign(self, order_id, driver_id):
        from orders.views import assign_driver_view

        return assign_driver_view(
            as_dispatcher(RequestFactory().post("/assign_driver", {"order_id": order_id, "driver_id": driver_id}))
        )

    def setUp(self):
        super().setUp()
        add_order(order_city="Davis", order_id="u1", customer_name="C", order_time=self.BASE)
        self.driver = self._driver("D1")

    def test_card_becomes_a_normal_queue_card(self):
        before = dashboard.pending_cards()
        self.assertTrue(before[0]["unassigned"])
        self._assign("u1", self.driver.pk)
        after = dashboard.pending_cards()
        self.assertFalse(after[0]["unassigned"])
        self.assertEqual(after[0]["eta_start_value"], "14:30")

    def test_already_assigned_order_is_a_404(self):
        self._assign("u1", self.driver.pk)
        with self.assertRaises(Http404):
            self._assign("u1", self.driver.pk)

    def test_inactive_driver_is_a_404(self):
        Driver.objects.filter(pk=self.driver.pk).update(is_active=False)
        with self.assertRaises(Http404):
            self._assign("u1", self.driver.pk)
        self.assertIsNone(Order.objects.get(order_id="u1").driver_id)

    def test_sent_order_is_assigned_without_texting_anyone(self):
        Order.objects.filter(order_id="u1").update(status=Order.Status.SENT)
        with patch("orders.tasks.send_message_task") as send_task, patch("orders.tasks.fill_order_notes_task") as notes:
            self._assign("u1", self.driver.pk)
        send_task.delay.assert_not_called()
        notes.delay.assert_not_called()
        order = Order.objects.get(order_id="u1")
        self.assertEqual((order.driver_id, order.status), (self.driver.pk, Order.Status.SENT))
        self.assertIsNone(order.eta_start)

    def test_no_text_customer_gets_notes_and_is_marked_sent_on_assignment(self):
        Order.objects.filter(order_id="u1").update(customer_name="Mohsen Awara", payment_type="Cash", webjoint_id=WJ_ID)
        with patch("orders.tasks.fill_order_notes_task") as notes, patch("orders.tasks.send_message_task") as send_task:
            self._assign("u1", self.driver.pk)
        notes.delay.assert_called_once()
        self.assertEqual(notes.delay.call_args.args[0], WJ_ID)
        self.assertIn("ETA:", notes.delay.call_args.args[1])
        send_task.delay.assert_not_called()
        self.assertEqual(Order.objects.get(order_id="u1").status, Order.Status.SENT)

    def test_no_text_customer_still_awaiting_payment_defers_the_note_to_the_lookup(self):
        Order.objects.filter(order_id="u1").update(
            customer_name="Mohsen Awara", payment_type="", status=Order.Status.AWAITING_PAYMENT
        )
        with patch("orders.tasks.fill_order_notes_task") as notes, patch(
            "orders.tasks.resolve_payment_type_task"
        ) as resolve:
            self._assign("u1", self.driver.pk)
        notes.delay.assert_not_called()
        resolve.delay.assert_called_once_with("u1", write_notes=True)


class TestQueueUsesStatus(TestCase):
    def _order(self, order_id, status, **extra):
        fields = {"payment_type": "Cash", **extra}
        driver = Driver.objects.get_or_create(name="D1", defaults={"is_active": True})[0]
        return Order.objects.create(
            driver=driver,
            order_id=order_id,
            customer_name=f"Cust {order_id}",
            customer_phone="15305551234",
            city="Davis",
            order_date=datetime(2026, 7, 20, 14, 0, 0),
            eta_start=datetime(2026, 7, 20, 14, 30, 0),
            eta_end=datetime(2026, 7, 20, 15, 30, 0),
            status=status,
            **fields,
        )

    def test_only_queue_statuses_show_in_the_queue(self):
        self._order("q1", Order.Status.AWAITING_APPROVAL)
        self._order("q2", Order.Status.AWAITING_PAYMENT, payment_type="")
        self._order("s1", Order.Status.SENT)
        self._order("c1", Order.Status.COMPLETED)
        self._order("x1", Order.Status.CANCELLED)
        self._order("sh", Order.Status.AWAITING_APPROVAL, is_shell=True)
        ids = set(queries.pending_orders().values_list("order_id", flat=True))
        self.assertEqual(ids, {"q1", "q2"})

    def test_send_refuses_an_order_that_is_already_sent(self):
        from orders.views import send_dispatch_msg_view

        self._order("s1", Order.Status.SENT)
        request = RequestFactory().post("/send_dispatch_msg", {"order_id": "s1"})
        with patch("orders.tasks.send_message_task") as send_task, self.assertRaises(Http404):
            send_dispatch_msg_view(as_dispatcher(request))
        send_task.delay.assert_not_called()

    def test_unlink_puts_companion_back_in_the_queue(self):
        from orders.views import unlink_order_view

        primary = self._order("p1", Order.Status.AWAITING_APPROVAL)
        self._order("f1", Order.Status.SENT, linked_order=primary)
        unlink_order_view(as_dispatcher(RequestFactory().post("/unlink_order", {"order_id": "f1"})))
        companion = Order.objects.get(order_id="f1")
        self.assertEqual(companion.status, Order.Status.AWAITING_APPROVAL)
        self.assertIsNone(companion.linked_order)

    def test_unlink_keeps_send_held_when_payment_type_is_still_missing(self):
        from orders.views import unlink_order_view

        primary = self._order("p1", Order.Status.AWAITING_APPROVAL)
        self._order(
            "f1",
            Order.Status.SENT,
            linked_order=primary,
        )
        Order.objects.filter(order_id="f1").update(payment_type="")
        unlink_order_view(as_dispatcher(RequestFactory().post("/unlink_order", {"order_id": "f1"})))
        self.assertEqual(Order.objects.get(order_id="f1").status, Order.Status.AWAITING_PAYMENT)

    def test_resolve_payment_task_moves_order_on_to_awaiting_approval(self):
        from orders.tasks import resolve_payment_type_task

        self._order("w1", Order.Status.AWAITING_PAYMENT, payment_type="", weedmaps_order_id="49018286")
        with patch("orders.tasks.get_wm_payment_type", return_value="Cash"):
            resolve_payment_type_task("w1")
        order = Order.objects.get(order_id="w1")
        self.assertEqual((order.payment_type, order.status), ("Cash", Order.Status.AWAITING_APPROVAL))

    def test_resolve_payment_task_leaves_a_sent_order_alone(self):
        from orders.tasks import resolve_payment_type_task

        self._order("w1", Order.Status.SENT, payment_type="", weedmaps_order_id="49018286")
        with patch("orders.tasks.get_wm_payment_type", return_value="Cash"):
            resolve_payment_type_task("w1")
        self.assertEqual(Order.objects.get(order_id="w1").status, Order.Status.SENT)
