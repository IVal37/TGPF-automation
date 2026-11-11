"""The Restock tab: completed delivery orders the dispatcher ticks off, and the running list of what to restock."""

from __future__ import annotations

from typing import Dict, Iterable

from django.db.models import QuerySet, Sum

from orders.models import Order, OrderItem, OrderStatus


def unreconciled_orders() -> QuerySet:
    """Completed delivery orders not yet ticked under "Restock Time"."""
    return Order.objects.filter(status=OrderStatus.COMPLETED, is_shell=False, restock_reconciled=False)


def restock_context() -> Dict:
    """The orders waiting to be reconciled, and what to restock (reconciled orders' items,
    summed, until "Complete Restock" clears them)."""
    rows = (
        OrderItem.objects.filter(order__restock_reconciled=True, order__restock_cleared=False)
        .values("product_name")
        .annotate(quantity=Sum("quantity"))
        .order_by("product_name")
    )
    return {
        "completed_orders": list(unreconciled_orders().order_by("pk")),
        "restock_items": {row["product_name"]: {"quantity": row["quantity"]} for row in rows},
    }


def reconcile(order_ids: Iterable[str]) -> None:
    unreconciled_orders().filter(order_id__in=list(order_ids)).update(restock_reconciled=True)


def clear_restock_list() -> None:
    Order.objects.filter(restock_reconciled=True, restock_cleared=False).update(restock_cleared=True)
