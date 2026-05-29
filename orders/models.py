# imports from std lib
from datetime import datetime

# imports from django
from django.db import models


class Driver(models.Model):
    name = models.CharField(max_length=255)
    current_city = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=False)

    def get_id(self) -> int:
        return self.pk

    def get_current_city(self) -> str:
        return self.current_city

    def set_current_city(self, city: str) -> None:
        self.current_city = city
        self.save()

    def add_order(self, order) -> None:
        order.driver = self
        order.save()

    def complete_order(self, order_id) -> None:
        self.orders.filter(order_id=order_id).update(driver=None)

    def get_active_orders(self):
        return list(self.orders.all())

    def __str__(self):
        return self.name


class Order(models.Model):
    order_id = models.CharField(max_length=100, unique=True)
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
    source = models.CharField(max_length=100, blank=True)
    payment_type = models.CharField(max_length=100, blank=True)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    completed = models.BooleanField(default=False)

    def get_id(self):
        return self.order_id

    def get_city(self) -> str:
        return self.city

    def get_created_at(self) -> datetime:
        return self.order_date

    def set_etas(self, start: datetime, end: datetime) -> None:
        self.eta_start = start
        self.eta_end = end
        self.save()

    def __str__(self):
        if self.eta_start and self.eta_end:
            start = self.eta_start.strftime("%-I:%M")
            end = self.eta_end.strftime("%-I:%M")
            return f"{start}-{end}"
        return f"Order {self.order_id}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.product_name} (x{self.quantity})"