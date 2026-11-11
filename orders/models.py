# imports from std lib
from datetime import datetime

# imports from django
from django.db import models
from django.utils import timezone

# imports from project
from orders.services.time import format_time


class Driver(models.Model):
    name = models.CharField(max_length=255)
    current_city = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=False)
    # when current_city was last set; on startup a city from an earlier day is
    # reset to Davis, one set today is kept
    location_updated_at = models.DateTimeField(null=True, blank=True)

    def set_current_city(self, city: str) -> None:
        self.current_city = city
        self.location_updated_at = timezone.now()
        self.save(update_fields=["current_city", "location_updated_at"])

    def add_order(self, order: "Order") -> None:
        order.driver = self
        order.save(update_fields=["driver"])

    def __str__(self) -> str:
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        # Weedmaps order whose payment type is still being looked up
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        # in the Dispatch Queue for a dispatcher to approve and send
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
        # the dispatch text is queued or being sent; set before the send task runs
        # and moved to SENT by it, so a second Send can't text the customer twice
        SENDING = "sending", "Sending"
        # dispatch text sent (or never needed); the order is out with a driver
        SENT = "sent", "Sent"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        # walk-in 3rd St sale, finished when rung up
        STOREFRONT = "storefront", "Storefront"

    order_id = models.CharField(max_length=100, unique=True)
    # WebJoint's string key for the order (<company>-<facility>-<customer>-<id>),
    # used to open exactly this order in the WebJoint admin; blank for orders
    # created before it was stored
    webjoint_id = models.CharField(max_length=100, blank=True)
    # the number Weedmaps shows as "#49018286" (the webhook's weedmapsOrderId);
    # blank for orders that did not come through Weedmaps
    weedmaps_order_id = models.CharField(max_length=20, blank=True)
    customer_name = models.CharField(max_length=255, blank=True)
    customer_phone = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=255)
    order_date = models.DateTimeField()
    driver = models.ForeignKey(
        Driver, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="orders"
    )
    eta_start = models.DateTimeField(null=True, blank=True)
    eta_end = models.DateTimeField(null=True, blank=True)
    # the delivery window a customer picked for a scheduled order, kept so it can
    # still be applied if the order is assigned to a driver after it arrives
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=100, blank=True)
    payment_type = models.CharField(max_length=100, blank=True)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices,
        default=Status.AWAITING_APPROVAL, db_index=True,
    )
    # the last browser task for this order that failed for good (after its
    # automatic retries), shown on the dispatch card with Retry / Dismiss.
    # last_error_task + last_error_args let Retry re-queue the same task;
    # last_error_after_commit means it failed at or after the irreversible
    # click, so it may have gone through and Retry asks for a confirm
    last_error = models.TextField(blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error_task = models.CharField(max_length=100, blank=True)
    last_error_args = models.JSONField(default=dict, blank=True)
    last_error_after_commit = models.BooleanField(default=False)
    # placeholder order (city only) that tracks where a driver is heading
    is_shell = models.BooleanField(default=False)
    # restock tab: a completed delivery order is "reconciled" once the dispatcher
    # ticks it under Restock Time (its items join the restock list), and
    # "cleared" once Complete Restock empties that list
    restock_reconciled = models.BooleanField(default=False)
    restock_cleared = models.BooleanField(default=False)
    # set on the companion (FSA/FSM) order, pointing at the real customer's
    # order it's actually part of — see pricing.effective_total
    linked_order = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='linked_companions'
    )

    def set_etas(self, start: datetime, end: datetime) -> None:
        self.eta_start = start
        self.eta_end = end
        self.save(update_fields=["eta_start", "eta_end"])

    def __str__(self) -> str:
        if self.eta_start and self.eta_end:
            return f"{format_time(self.eta_start)}-{format_time(self.eta_end)}"
        return f"Order {self.order_id}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self) -> str:
        return f"{self.product_name} (x{self.quantity})"


OrderStatus = Order.Status

# statuses that are still in the Dispatch Queue (waiting on a dispatcher)
QUEUE_STATUSES = (OrderStatus.AWAITING_PAYMENT, OrderStatus.AWAITING_APPROVAL)
# statuses that are still open: queued, or sent and out with a driver
OPEN_STATUSES = QUEUE_STATUSES + (OrderStatus.SENDING, OrderStatus.SENT)


# a message for the dashboard that isn't about one order (a webhook registration
# result, a merchant-pay link to send by hand, a product upload that failed)
class Notice(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "Info"
        ERROR = "error", "Error"

    message = models.TextField()
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    created_at = models.DateTimeField(default=timezone.now)
    dismissed = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.message[:60]


# ── Business config (editable in Django admin) ────────────────────────────────

# a city we deliver to and its order minimum (before taxes, after discounts)
class City(models.Model):
    name = models.CharField(max_length=255, unique=True)
    minimum = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "cities"

    def __str__(self) -> str:
        return f"{self.name} (${self.minimum})"


# special handling for named customers: gets the merchant-pay link, or is never texted
class CustomerFlag(models.Model):
    name = models.CharField(max_length=255, help_text='Full name, e.g. "Imran Rahim"')
    merchant_pay = models.BooleanField(default=False)
    no_text = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# a delivery address that is not a normal customer: "blocked" orders are ignored
# entirely, "storefront" orders are walk-in sales at the store. A row matches
# when the number is equal, the street contains street_contains (any case), and
# the city and zip are equal (blank = any).
class SpecialAddress(models.Model):
    class Kind(models.TextChoices):
        BLOCKED = "blocked", "Blocked (ignore the order)"
        STOREFRONT = "storefront", "Storefront (walk-in sale)"

    number = models.CharField(max_length=20)
    street_contains = models.CharField(max_length=255)
    city = models.CharField(max_length=255, blank=True)
    zip = models.CharField(max_length=20, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)

    class Meta:
        verbose_name_plural = "special addresses"

    def __str__(self) -> str:
        return f"{self.number} …{self.street_contains}… {self.city} {self.zip} ({self.kind})".replace("  ", " ")


# minutes to drive between two cities. Each pair is stored once with the names in
# alphabetical order (use orders.services.travel.get_travel_time to look one up).
class TravelTime(models.Model):
    class Source(models.TextChoices):
        STATIC = "static", "Static (typed in)"
        GOOGLE = "google", "Google Maps"

    city_a = models.CharField(max_length=255)
    city_b = models.CharField(max_length=255)
    minutes = models.PositiveIntegerField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.STATIC)
    fetched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["city_a", "city_b"]
        constraints = [models.UniqueConstraint(fields=["city_a", "city_b"], name="unique_travel_pair")]

    def __str__(self) -> str:
        return f"{self.city_a} - {self.city_b}: {self.minutes} min ({self.source})"
