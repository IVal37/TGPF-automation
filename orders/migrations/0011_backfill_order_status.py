from django.db import migrations

BATCH = 500


def status_for(is_storefront, completed, dispatch_sent, eta_start, is_shell):
    """The status an order with the old boolean flags should have. First match wins."""
    if is_storefront:
        return "storefront"
    if completed:
        return "completed"
    if dispatch_sent:
        return "sent"
    if is_shell:
        # an open placeholder is with a driver and never queued
        return "sent"
    if eta_start is not None:
        return "awaiting_approval"
    return "awaiting_payment"


def backfill(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    pending = []
    for order in Order.objects.all().iterator():
        order.is_shell = order.order_id.startswith("shell_")
        order.status = status_for(
            order.is_storefront, order.completed, order.dispatch_sent,
            order.eta_start, order.is_shell,
        )
        pending.append(order)
        if len(pending) >= BATCH:
            Order.objects.bulk_update(pending, ["status", "is_shell"])
            pending = []
    if pending:
        Order.objects.bulk_update(pending, ["status", "is_shell"])


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0010_order_status_is_shell"),
    ]

    # reverse is a no-op: completed / dispatch_sent / is_storefront are still
    # written in step with status, so they are already correct
    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
