# imports from std lib
import json
from datetime import datetime
from decimal import Decimal

# imports from django
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# imports from project
from orders.models import Order
from orders.services.engine import add_order, complete_order
from orders.services.dispatch import extract_msg_info, get_dispatch_msg
from orders.services.scrapers.talkroute import send_message

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
    send_message(msg_dict["phone"], dispatch_msg)
    return JsonResponse({"status": "ok"})

@csrf_exempt
@require_http_methods(["POST"])
def complete_order_view(request):
    data = json.loads(request.body)
    complete_order(order_id=data["id"])
    return JsonResponse({"status": "ok"})
