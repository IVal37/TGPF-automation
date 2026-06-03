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
from django.conf import settings
from orders.models import Driver, Order
from orders.services.dispatch import extract_msg_info, get_dispatch_msg
from orders.services.scrapers.talkroute import send_message
from orders.services.note import get_order_notes
from orders.services.scrapers.webjoint import fill_order_notes
from orders.constants import BLOCKED_ADDRESSES, CITY_MINS
from orders.services.engine import add_driver, add_order, add_shell_order, cancel_order, complete_order, delete_driver
from orders.services.state import active_drivers, completed_orders, drivers_by_id, restock_items

def order_list(request):
    return render(request, 'orders/order_list.html', {
        'completed_orders': completed_orders,
        'restock_items': restock_items,
        'active_drivers': active_drivers,
        'cities': sorted(CITY_MINS.keys()),
    })

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
    shipping = data.get("shipping", {})
    full_street = f"{shipping.get('number', '').strip()} {shipping.get('street', '').strip()}".strip()
    delivery_key = (full_street, shipping.get("zip", "").strip())
    if delivery_key in BLOCKED_ADDRESSES:
        return JsonResponse({"status": "ok"})
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
    if not settings.TEST_MODE:
        send_message(msg_dict["phone"], dispatch_msg)
        fill_order_notes(get_order_notes(msg_dict))
    return JsonResponse({"status": "ok"})

@csrf_exempt
@require_http_methods(["POST"])
def complete_order_view(request):
    data = json.loads(request.body)
    complete_order(order_id=data["id"])
    return JsonResponse({"status": "ok"})

@csrf_exempt
@require_http_methods(["POST"])
def cancel_order_view(request):
    data = json.loads(request.body)
    cancel_order(order_id=data["id"])
    return JsonResponse({"status": "ok"})

@csrf_exempt
@require_http_methods(["POST"])
def reconcile_restock(request):
    selected_ids = set(request.POST.getlist("order_ids"))
    to_remove = []
    for entry in completed_orders:
        if entry["order_id"] in selected_ids:
            for item in entry["items"]:
                name = item["product_name"]
                if name in restock_items:
                    restock_items[name]["quantity"] += item["quantity"]
                else:
                    restock_items[name] = {"quantity": item["quantity"], "price": item["price"]}
            to_remove.append(entry["order_id"])
    completed_orders[:] = [e for e in completed_orders if e["order_id"] not in to_remove]
    return redirect("/")

@csrf_exempt
@require_http_methods(["POST"])
def clear_restock(request):
    restock_items.clear()
    return redirect("/")

@csrf_exempt
@require_http_methods(["POST"])
def add_shell_order_view(request):
    city = request.POST.get("city")
    if city:
        add_shell_order(city)
    return redirect("/")

@csrf_exempt
@require_http_methods(["POST"])
def set_driver_location_view(request):
    driver_id = request.POST.get("driver_id")
    city = request.POST.get("city")
    if driver_id and city:
        drivers_by_id[int(driver_id)].set_current_city(city)
    return redirect("/")
