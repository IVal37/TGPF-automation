# imports from django
from rest_framework import serializers

# imports from project
from orders.models import Driver, Order, OrderItem


class DriverSerializer(serializers.ModelSerializer):
    class Meta:
        model = Driver
        fields = ["id", "name", "current_city"]


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ["id", "product_name", "quantity", "price"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    driver = DriverSerializer(read_only=True)

    class Meta:
        model = Order
        fields = ["id", "order_id", "customer_name", "city", "order_date", "driver", "eta_start", "eta_end", "items"]
