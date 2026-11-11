"""What the dashboard shows: the Dispatch Queue cards and the driver panel (read side)."""
from __future__ import annotations

from typing import Dict, List

from django.db.models import Prefetch

from orders.models import QUEUE_STATUSES, Driver, Order, OrderStatus
from orders.services import queries
from orders.services.customers import is_fsa_fsm
from orders.services.hours import is_past_closing


def pending_cards() -> List[Dict]:
    """One dict per card on the Dispatch Queue tab, unassigned cards first (they need a driver
    before anything else), then by ETA. Everything a card needs is loaded in two queries."""
    orders = sorted(
        queries.pending_orders()
        .select_related("driver")
        .prefetch_related(Prefetch("linked_companions", queryset=Order.objects.order_by("pk"))),
        key=lambda o: (o.driver_id is not None, o.eta_start or o.order_date, o.pk),
    )
    # only FSA/FSM cards offer the "link to..." dropdown, and it never lists other FSA/FSM orders
    link_targets = [o for o in orders if not is_fsa_fsm(o.customer_name)]

    cards = []
    for order in orders:
        fsa_fsm = is_fsa_fsm(order.customer_name)
        unassigned = order.driver_id is None
        in_queue = order.status in QUEUE_STATUSES
        companions = list(order.linked_companions.all())
        cards.append({
            "order": order,
            "unassigned": unassigned,
            # waiting for a dispatcher (approve + send); other cards are only here
            # because they are sending right now or have an error to show
            "in_queue": in_queue,
            "sending": order.status == OrderStatus.SENDING,
            # an unassigned order that was texted anyway (under-min, or its driver
            # was switched off after the dispatch text went out)
            "already_texted": unassigned and order.status in (OrderStatus.SENDING, OrderStatus.SENT),
            "eta_start_value": order.eta_start.strftime("%H:%M") if order.eta_start else "",
            "eta_end_value": order.eta_end.strftime("%H:%M") if order.eta_end else "",
            "past_closing": in_queue and bool(order.eta_start) and is_past_closing(order.eta_start),
            "is_fsa_fsm": fsa_fsm,
            # set once an FSA/FSM order is linked to this (a real customer's) order as the
            # second part of a legal-limit split
            "linked_companion": companions[0] if companions else None,
            "link_targets": link_targets if fsa_fsm else [],
        })
    return cards


def driver_panel() -> List[Dict]:
    """Every driver (on shift or not) with what they are currently out delivering."""
    return [
        {"driver": driver, "out_for_delivery": list(queries.out_for_delivery(driver.pk)) if driver.is_active else []}
        for driver in Driver.objects.order_by("pk")
    ]
