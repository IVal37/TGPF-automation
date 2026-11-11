from datetime import datetime

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase


def _status_for():
    import importlib

    return importlib.import_module("orders.migrations.0011_backfill_order_status").status_for


class TestBackfillMapping(SimpleTestCase):
    ETA = datetime(2026, 7, 20, 14, 30)

    def check(self, expected, *, storefront=False, completed=False, sent=False, eta=None, shell=False):
        self.assertEqual(_status_for()(storefront, completed, sent, eta, shell), expected)

    def test_storefront_wins_over_everything(self):
        self.check("storefront", storefront=True, completed=True, sent=True, eta=self.ETA)

    def test_completed_wins_over_sent(self):
        self.check("completed", completed=True, sent=True, eta=self.ETA)

    def test_sent_when_dispatch_sent(self):
        self.check("sent", sent=True, eta=self.ETA)

    def test_awaiting_approval_when_it_has_an_eta(self):
        self.check("awaiting_approval", eta=self.ETA)

    def test_awaiting_payment_when_no_eta(self):
        self.check("awaiting_payment")

    def test_open_shell_is_sent_and_closed_shell_completed(self):
        self.check("sent", shell=True)
        self.check("completed", shell=True, completed=True)


class TestBackfillMigration(TransactionTestCase):
    """Runs the real migrations: seed rows at 0009, migrate to 0011, check each row."""

    migrate_from = [("orders", "0009_order_weedmaps_order_id")]
    migrate_to = [("orders", "0011_backfill_order_status")]

    ROWS = {
        # order_id: (flags, expected status, expected is_shell)
        "store": (dict(is_storefront=True, completed=True, dispatch_sent=True), "storefront", False),
        "done": (dict(completed=True, dispatch_sent=True, eta=True), "completed", False),
        "cancelled-before": (dict(completed=True, eta=True), "completed", False),
        "sent": (dict(dispatch_sent=True, eta=True), "sent", False),
        "queued": (dict(eta=True), "awaiting_approval", False),
        "no-eta": (dict(), "awaiting_payment", False),
        "shell_open": (dict(), "sent", True),
        "shell_closed": (dict(completed=True), "completed", True),
    }

    def tearDown(self):
        # leave the test database fully migrated for whichever test runs next
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_each_old_combination_maps_to_the_right_status(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        Order = executor.loader.project_state(self.migrate_from).apps.get_model("orders", "Order")
        for order_id, (flags, _, _) in self.ROWS.items():
            flags = dict(flags)
            eta = datetime(2026, 7, 20, 14, 30) if flags.pop("eta", False) else None
            Order.objects.create(
                order_id=order_id, city="Davis", order_date=datetime(2026, 7, 20, 14, 0), eta_start=eta, **flags
            )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        Order = executor.loader.project_state(self.migrate_to).apps.get_model("orders", "Order")
        for order_id, (_, status, is_shell) in self.ROWS.items():
            row = Order.objects.get(order_id=order_id)
            self.assertEqual((row.status, row.is_shell), (status, is_shell), order_id)


class TestBackfillRestockFlags(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_orders_do_not_flood_the_restock_tab(self):
        before, after = (
            [("orders", "0011_backfill_order_status")],
            [("orders", "0012_restock_flags_driver_location_updated_at")],
        )
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        Old = executor.loader.project_state(before).apps.get_model("orders", "Order")
        Old.objects.create(
            order_id="old", city="Davis", order_date=datetime(2026, 7, 1), status="completed", completed=True
        )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        New = executor.loader.project_state(after).apps.get_model("orders", "Order")
        row = New.objects.get(order_id="old")
        self.assertEqual((row.restock_reconciled, row.restock_cleared), (True, True))
        new_row = New.objects.create(order_id="new", city="Davis", order_date=datetime(2026, 7, 2))
        self.assertEqual((new_row.restock_reconciled, new_row.restock_cleared), (False, False))
