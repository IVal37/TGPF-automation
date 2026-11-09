# imports from std lib
import json
import logging
from datetime import datetime
from decimal import Decimal

# imports from django
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# imports from project
from django.conf import settings
from orders.models import Driver, Order
from orders.services.dispatch import (
    build_dispatch_msg_from_order, build_reschedule_msg_from_order,
    extract_msg_info, get_dispatch_msg, is_fsa_fsm, is_merchant_pay_customer,
    is_under_min, is_weedmaps_source,
)
from orders.services.scrapers.weedmaps import get_wm_payment_type
from orders.services.note import get_order_notes
from orders.tasks import (
    create_product_task, dispatch_to_driver_task, fill_order_notes_task,
    pull_from_chiles_task, send_merchant_pay_link_task, send_message_task,
)
from orders.services.invoice import extract_products_from_invoice
from orders.services.time import parse_webhook_time
from orders.constants import BLOCKED_ADDRESSES, CITY_MINS
from orders.services.hours import is_past_closing
from orders.services.engine import (
    add_driver, add_order, add_shell_order, add_storefront_order,
    cancel_order, complete_order, delete_driver,
)
from orders.services.state import active_drivers, completed_orders, drivers_by_id, restock_items
from orders.webhooks import client_ip, require_webhook_key

logger = logging.getLogger(__name__)

# orders with a real ETA window still waiting on a dispatcher to approve/send
def _pending_queryset():
    return Order.objects.filter(completed=False, dispatch_sent=False, eta_start__isnull=False)

def _mark_dispatch_sent(order_id: str) -> None:
    Order.objects.filter(order_id=order_id).update(dispatch_sent=True)

def _pending_orders():
    orders_list = list(_pending_queryset().order_by('eta_start'))
    result = []
    for order in orders_list:
        order_is_fsa_fsm = is_fsa_fsm(order.customer_name)
        result.append({
            "order": order,
            "eta_start_value": order.eta_start.strftime("%H:%M"),
            "eta_end_value": order.eta_end.strftime("%H:%M"),
            "past_closing": is_past_closing(order.eta_start),
            "is_fsa_fsm": order_is_fsa_fsm,
            # only set once this order (a real customer's) has an FSA/FSM
            # order linked to it as the second part of a legal-limit split
            "linked_companion": Order.objects.filter(linked_order=order).first(),
            # only FSA/FSM cards offer the "link to..." dropdown, and it
            # never lists other FSA/FSM orders as a target
            "link_targets": (
                [o for o in orders_list if o.order_id != order.order_id and not is_fsa_fsm(o.customer_name)]
                if order_is_fsa_fsm else []
            ),
        })
    return result

@login_required
def pending_orders_partial(request):
    """Renders just the dispatch-queue cards, polled by the dashboard's JS to
    auto-refresh that tab without a full page reload."""
    return render(request, 'orders/_pending_orders.html', {'pending_orders': _pending_orders()})

@login_required
def restock_partial(request):
    """Renders just the completed-orders/restock-list content, polled by the
    dashboard's JS to auto-refresh that tab without a full page reload."""
    return render(request, 'orders/_restock.html', {
        'completed_orders': completed_orders,
        'restock_items': restock_items,
    })

@login_required
@require_http_methods(["POST"])
def clear_dispatch_queue_view(request):
    """Dismisses every order currently sitting in the Dispatch Queue without
    texting anyone or writing any WebJoint notes — a cleanup/testing utility,
    not a bulk-send. Real pending orders cleared this way never get their
    dispatch SMS, so the frontend confirms before submitting."""
    _pending_queryset().update(dispatch_sent=True)
    return redirect("/")

@login_required
def order_list(request):
    pending_orders = _pending_orders()
    return render(request, 'orders/order_list.html', {
        'completed_orders': completed_orders,
        'restock_items': restock_items,
        'active_drivers': active_drivers,
        'cities': sorted(CITY_MINS.keys()),
        'pending_orders': pending_orders,
    })

@login_required
@require_http_methods(["POST"])
def add_dummy_driver(request):
    count = Driver.objects.count()
    driver = Driver.objects.create(name=f"Driver {count + 1}")
    add_driver(driver.pk)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def delete_driver_view(request):
    driver_id = request.POST.get("driver_id")
    if driver_id:
        delete_driver(int(driver_id))
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def complete_order_manual(request):
    order_id = request.POST.get("order_id")
    if order_id:
        complete_order(order_id=order_id)
    return redirect("/")

# the webhook's "delivery" field is a bare fee amount, e.g. "delivery": 3.99
def _extract_delivery_fee(data) -> Decimal:
    return Decimal(str(data.get("delivery", 0)))

@csrf_exempt
def legacy_webhook(request):
    """The old bare /new_order, /complete_order and /cancel_order paths. They no
    longer do anything - WebJoint must call hooks/<WEBHOOK_SECRET>/... instead -
    but they stay routed so a stale registration shows up in the log. Exempt from
    CSRF only so a POST gets this logged 403 rather than a bare CSRF failure."""
    logger.warning("webhook hit on retired path %s from %s", request.path, client_ip(request))
    return HttpResponseForbidden()

@csrf_exempt
@require_webhook_key
@require_http_methods(["POST"])
def new_order(request):
    data = json.loads(request.body)
    shipping = data.get("shipping", {})
    full_street = f"{shipping.get('number', '').strip()} {shipping.get('street', '').strip()}".strip()
    delivery_key = (full_street, shipping.get("zip", "").strip())
    street_lower = shipping.get('street', '').strip().lower()
    is_third_st = (
        shipping.get('number', '').strip() == '526'
        and shipping.get('city', '').strip() == 'Davis'
        and any(w in street_lower for w in ('third', '3rd'))
    )
    # BLOCKED_ADDRESSES stays a general "don't create an order at all" list for
    # anything else that ends up in it — 3rd St gets its own explicit routing
    # below instead, since (unlike a truly blocked address) it's a real sale
    # that needs to be tracked for the storefront restock digest.
    if delivery_key in BLOCKED_ADDRESSES and not is_third_st:
        return JsonResponse({"status": "ok"})

    # fields shared by storefront and delivery orders
    order_fields = dict(
        order_id=str(data["id"]),
        customer_name=data["customer"]["name"],
        order_time=parse_webhook_time(data["created"]),
        source=data.get("source", ""),
        discount=Decimal(str(data.get("discount", "0"))),
        subtotal=Decimal(str(data.get("subtotal", "0"))),
        total=Decimal(str(data.get("total", "0"))),
        delivery_fee=_extract_delivery_fee(data),
        items=data.get("details", []),
    )

    if is_third_st:
        # walk-in storefront pickup — no driver, no ETA, no dispatch SMS; just
        # persisted so the nightly 3rd St restock digest can pick it up
        add_storefront_order(**order_fields)
        return JsonResponse({"status": "ok"})

    add_order(
        order_city=data["shipping"]["city"],
        customer_phone=data["customer"]["phone"][1:],
        **order_fields,
    )
    msg_dict = extract_msg_info(data)
    if is_weedmaps_source(msg_dict["source"]) and not settings.RESTOCK_TEST_MODE:
        msg_dict["pay_type"] = get_wm_payment_type()
    # always computed: sets order.eta_start/eta_end via get_eta (including any
    # scheduled-order override) and routes under-min vs normal internally. For
    # normal (has-ETA) orders the returned text is discarded below — the real
    # send happens later from send_dispatch_msg_view, once a dispatcher has
    # approved or edited the ETA window.
    dispatch_msg = get_dispatch_msg(msg_dict)
    Order.objects.filter(order_id=msg_dict["id"]).update(payment_type=msg_dict["pay_type"])

    no_text = data["customer"]["name"].strip().lower() == "mohsen awara"
    under_min = is_under_min(msg_dict)

    if not settings.RESTOCK_TEST_MODE:
        if under_min:
            # no ETA window in this message, nothing to double-check — auto-send
            if not no_text:
                send_message_task.delay(msg_dict["phone"], dispatch_msg)
            fill_order_notes_task.delay("reached out about under min")
            _mark_dispatch_sent(msg_dict["id"])
        elif no_text:
            # excluded customer: we never text them, so there's nothing to hold
            # for approval — write notes with the system-computed window now
            order = Order.objects.get(order_id=msg_dict["id"])
            fill_order_notes_task.delay(get_order_notes(order))
            _mark_dispatch_sent(msg_dict["id"])
        # else: normal dispatch with a real ETA window — leave dispatch_sent=False
        # so it shows up in the Dispatch Queue tab for a human to approve/edit
        # and send (see send_dispatch_msg_view / send_reschedule_msg_view below).

        if is_merchant_pay_customer(msg_dict["name"], msg_dict["last_name"]) and not under_min:
            send_merchant_pay_link_task.delay()

        pull_from_chiles_task.delay()
        if data.get("status") == "Pending":
            dispatch_to_driver_task.delay()
    return JsonResponse({"status": "ok"})

@login_required
@require_http_methods(["POST"])
def send_dispatch_msg_view(request):
    order_id = request.POST.get("order_id")
    eta_start_str = request.POST.get("eta_start")
    eta_end_str = request.POST.get("eta_end")
    order = get_object_or_404(Order, order_id=order_id, dispatch_sent=False)

    if eta_start_str and eta_end_str:
        base_date = order.eta_start.date() if order.eta_start else datetime.now().date()
        eta_start = datetime.combine(base_date, datetime.strptime(eta_start_str, "%H:%M").time())
        eta_end = datetime.combine(base_date, datetime.strptime(eta_end_str, "%H:%M").time())
        order.set_etas(eta_start, eta_end)

    if not settings.RESTOCK_TEST_MODE:
        send_message_task.delay(order.customer_phone, build_dispatch_msg_from_order(order))
        fill_order_notes_task.delay(get_order_notes(order))

    _mark_dispatch_sent(order_id)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def send_reschedule_msg_view(request):
    order_id = request.POST.get("order_id")
    order = get_object_or_404(Order, order_id=order_id, dispatch_sent=False)

    if not settings.RESTOCK_TEST_MODE:
        send_message_task.delay(order.customer_phone, build_reschedule_msg_from_order(order))
        fill_order_notes_task.delay("reached out about rescheduling for tomorrow morning")

    _mark_dispatch_sent(order_id)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def link_order_view(request):
    """Marks an FSA/FSM order as the second part of a legal-limit split: it's
    fully absorbed into the primary order (no SMS, no independent Dispatch
    Queue entry of its own) — only a note goes into WebJoint identifying it
    as part of the primary customer's order. The primary's own SMS/notes then
    reflect the combined total once it's sent (see dispatch.get_effective_total)."""
    order_id = request.POST.get("order_id")
    primary_order_id = request.POST.get("primary_order_id")
    if not order_id or not primary_order_id:
        return redirect("/")

    companion = get_object_or_404(Order, order_id=order_id, completed=False, dispatch_sent=False)
    primary = get_object_or_404(Order, order_id=primary_order_id, completed=False, dispatch_sent=False)

    if not is_fsa_fsm(companion.customer_name):
        return redirect("/")

    companion.linked_order = primary
    companion.dispatch_sent = True
    companion.save()

    if not settings.RESTOCK_TEST_MODE:
        fill_order_notes_task.delay(f"second part of {primary.customer_name}'s order")

    return redirect("/")

@login_required
@require_http_methods(["POST"])
def unlink_order_view(request):
    """Reverses link_order_view — puts the FSA/FSM order back into the
    Dispatch Queue for normal handling."""
    order_id = request.POST.get("order_id")
    if not order_id:
        return redirect("/")

    companion = get_object_or_404(Order, order_id=order_id, completed=False)
    companion.linked_order = None
    companion.dispatch_sent = False
    companion.save()
    return redirect("/")

@csrf_exempt
@require_webhook_key
@require_http_methods(["POST"])
def complete_order_view(request):
    data = json.loads(request.body)
    complete_order(order_id=str(data["id"]), city=data.get("shipping", {}).get("city", ""))
    return JsonResponse({"status": "ok"})

@csrf_exempt
@require_webhook_key
@require_http_methods(["POST"])
def cancel_order_view(request):
    data = json.loads(request.body)
    cancel_order(order_id=data["id"])
    return JsonResponse({"status": "ok"})

@login_required
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

@login_required
@require_http_methods(["POST"])
def clear_restock(request):
    restock_items.clear()
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def add_shell_order_view(request):
    city = request.POST.get("city")
    if city:
        add_shell_order(city)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def add_products_from_invoice(request):
    invoice_file = request.FILES["invoice"]
    products = extract_products_from_invoice(invoice_file.read(), invoice_file.content_type)
    create_product_task.delay(products)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def set_driver_location_view(request):
    driver_id = request.POST.get("driver_id")
    city = request.POST.get("city")
    if driver_id and city:
        drivers_by_id[int(driver_id)].set_current_city(city)
    return redirect("/")
