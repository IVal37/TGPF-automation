"""The Restock tab: completed delivery orders the dispatcher ticks off, and the running list of what to restock."""

from __future__ import annotations

from typing import Dict, Iterable

from django.db.models import QuerySet, Sum

from orders.models import Order, OrderItem, OrderStatus


def unreconciled_orders() -> QuerySet:
    """Completed delivery orders not yet ticked under "Restock Time"."""
    return Order.objects.filter(status=OrderStatus.COMPLETED, is_shell=False, restock_reconciled=False)


def restock_items() -> Dict[str, int]:
    """What to restock, product -> quantity: the items of reconciled orders, summed, until
    "Complete Restock" clears them. The Restock tab and the delivery restock message both show
    exactly this, so they cannot disagree."""
    rows = (
        OrderItem.objects.filter(order__restock_reconciled=True, order__restock_cleared=False)
        .values("product_name")
        .annotate(quantity=Sum("quantity"))
        .order_by("product_name")
    )
    return {row["product_name"]: row["quantity"] for row in rows}


def restock_context() -> Dict:
    """The orders waiting to be reconciled, and what to restock."""
    return {
        "completed_orders": list(unreconciled_orders().order_by("pk")),
        "restock_items": {name: {"quantity": quantity} for name, quantity in restock_items().items()},
    }


def restock_message() -> str:
    """The delivery restock message: the Restock List as the dashboard shows it, one
    `Product xN` line each. Completed orders nobody has ticked off yet are not in that list, so
    they are counted underneath rather than silently missing."""
    lines = [f"{name} x{quantity}" for name, quantity in restock_items().items()] or ["No items to restock."]
    waiting = unreconciled_orders().count()
    if waiting:
        lines.append(f"({waiting} completed order{'s' if waiting != 1 else ''} not ticked off under Restock Time yet)")
    return "\n".join(lines)


def reconcile(order_ids: Iterable[str]) -> None:
    unreconciled_orders().filter(order_id__in=list(order_ids)).update(restock_reconciled=True)


def clear_restock_list() -> None:
    Order.objects.filter(restock_reconciled=True, restock_cleared=False).update(restock_cleared=True)
