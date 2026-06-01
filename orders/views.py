# imports from std lib
import json
from datetime import datetime
from decimal import Decimal

# imports from django
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# imports from project
from orders.models import Driver, Order
from orders.services.engine import add_driver, add_order, complete_order, delete_driver
from orders.services.dispatch import extract_msg_info, get_dispatch_msg
from orders.services.scrapers.talkroute import send_message
from orders.services.note import get_order_notes
from orders.services.scrapers.webjoint import fill_order_notes
from orders.constants import BLOCKED_ADDRESSES

def order_list(request):
    orders = Order.objects.filter(completed=False).prefetch_related('items').order_by('-order_date')
    drivers = Driver.objects.order_by('pk')
    return render(request, 'orders/order_list.html', {'orders': orders, 'drivers': drivers})

@csrf_exempt
@require_http_methods(["POST"])
def add_dummy_driver(request):
    count = Driver.objects.count()
    driver = Driver.objects.create(name=f"Driver {count + 1}")
    add_driver(driver.pk)
    return redirect("/")

@csrf_exempt
@require_http_methods(["POST"])
def delete_driver_view(request):
    driver_id = request.POST.get("driver_id")
    if driver_id:
        delete_driver(int(driver_id))
    return redirect("/")

@csrf_exempt
@require_http_methods(["POST"])
def complete_order_manual(request):
    order_id = request.POST.get("order_id")
    if order_id:
        complete_order(order_id=order_id)
    return redirect("/")

@csrf_exempt
@require_http_methods(["POST"])
def new_order(request):
    data = json.loads(request.body)
    add_order(
        order_city=data["shipping"]["city"],
        order_id=str(data["id"]),
        customer_name=data["customer"]["name"],
        order_time=datetime.fromisoformat(data["created"].replace("Z", "+00:00")),
        customer_phone=data["customer"]["phone"][1:],
        source=data.get("source", ""),
        discount=Decimal(str(data.get("discount", "0"))),
        subtotal=Decimal(str(data.get("subtotal", "0"))),
        total=Decimal(str(data.get("total", "0"))),
        items=data.get("details", []),
    )
    msg_dict = extract_msg_info(data)
    dispatch_msg = get_dispatch_msg(msg_dict)
    Order.objects.filter(order_id=msg_dict["id"]).update(payment_type=msg_dict["pay_type"])
    shipping = data.get("shipping", {})
    full_street = f"{shipping.get('number', '').strip()} {shipping.get('street', '').strip()}".strip()
    delivery_key = (full_street, shipping.get("zip", "").strip())
    if delivery_key not in BLOCKED_ADDRESSES:
        send_message(msg_dict["phone"], dispatch_msg)
    fill_order_notes(get_order_notes(msg_dict))
    return JsonResponse({"status": "ok"})

@csrf_exempt
@require_http_methods(["POST"])
def complete_order_view(request):
    data = json.loads(request.body)
    complete_order(order_id=data["id"])
    return JsonResponse({"status": "ok"})
