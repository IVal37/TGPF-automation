# imports from std lib
import json
import logging
from typing import Optional

# imports from django
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# imports from project
from django.conf import settings
from orders import tasks
from orders.constants import HOME_CITY
from orders.models import Driver, Notice, Order
from orders.services import dashboard, dispatch_queue, intake, queries, restock
from orders.services.config import cities
from orders.services.dispatch_queue import ActionRejected, NotFound
from orders.services.engine import (
    add_driver, add_shell_order, cancel_order, complete_order, delete_driver, remove_driver,
)
from orders.services.invoice import InvoiceRejected, check_upload, extract_products_from_invoice
from orders.services.webhook_payload import InvalidPayload
from orders.webhooks import client_ip, require_webhook_key

logger = logging.getLogger(__name__)

# The views only read the request, call one service and answer. The rules live in
# orders/services/. Dashboard views redirect back to the dashboard; when the action
# was refused they say why in a message banner (and the log) instead of failing silently.


def _done(request, problem: Optional[str] = None):
    """Back to the dashboard, showing `problem` (if any) in the message banner."""
    if problem:
        logger.warning("%s", problem)
        messages.warning(request, problem, fail_silently=True)
    return redirect("order_list")


def _run(request, action, *args, **kwargs):
    """Run a dispatch_queue action: unknown order -> 404, refused -> a message."""
    try:
        action(*args, **kwargs)
    except NotFound as e:
        raise Http404(str(e)) from None
    except ActionRejected as e:
        return _done(request, str(e))
    return _done(request)


def _pk(value) -> int:
    """An id from a form, or 404 if it is missing or not a number."""
    if not str(value).isdigit():
        raise Http404("bad id")
    return int(value)


# ── Dashboard pages ──────────────────────────────────────────────────────────

@login_required
def order_list(request):
    return render(request, 'orders/order_list.html', {
        **restock.restock_context(),
        'active_drivers': list(queries.active_drivers()),
        'drivers': dashboard.driver_panel(),
        'notices': Notice.objects.filter(dismissed=False).order_by('-created_at')[:10],
        'cities': cities(),
        'pending_orders': dashboard.pending_cards(),
    })


@login_required
def pending_orders_partial(request):
    """Just the dispatch-queue cards, polled by the dashboard's JS to auto-refresh that tab."""
    return render(request, 'orders/_pending_orders.html', {
        'pending_orders': dashboard.pending_cards(),
        'active_drivers': list(queries.active_drivers()),
    })


@login_required
def restock_partial(request):
    """Just the completed-orders/restock-list content, polled by the dashboard's JS."""
    return render(request, 'orders/_restock.html', restock.restock_context())


# ── Drivers ──────────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["POST"])
def add_driver_view(request):
    """Creates a driver (starting the day in HOME_CITY) and puts them on shift."""
    name = request.POST.get("name", "").strip()[:255]
    if not name:
        return _done(request, "Enter a driver name.")
    driver = Driver.objects.create(name=name)
    add_driver(driver.pk)
    driver.set_current_city(HOME_CITY)
    return _done(request)


@login_required
@require_http_methods(["POST"])
def toggle_driver_view(request):
    """Switches a driver on or off shift. Switching one off unassigns their open orders."""
    driver = get_object_or_404(Driver, pk=_pk(request.POST.get("driver_id")))
    if driver.is_active:
        remove_driver(driver.pk)
    else:
        add_driver(driver.pk)
    return _done(request)


@login_required
@require_http_methods(["POST"])
def delete_driver_view(request):
    driver_id = request.POST.get("driver_id")
    if driver_id:
        delete_driver(_pk(driver_id))
    return _done(request)


@login_required
@require_http_methods(["POST"])
def set_driver_location_view(request):
    driver_id = request.POST.get("driver_id")
    city = request.POST.get("city")
    if driver_id and city:
        get_object_or_404(Driver, pk=_pk(driver_id), is_active=True).set_current_city(city)
    return _done(request)


@login_required
@require_http_methods(["POST"])
def add_shell_order_view(request):
    city = request.POST.get("city")
    if not city:
        return _done(request)
    try:
        add_shell_order(city)
    except ValueError:
        return _done(request, f"No driver is on shift, so the placeholder stop for {city} was not added.")
    return _done(request)


# ── Dispatch Queue actions ───────────────────────────────────────────────────

@login_required
@require_http_methods(["POST"])
def send_dispatch_msg_view(request):
    return _run(
        request, dispatch_queue.send_dispatch,
        request.POST.get("order_id"), request.POST.get("eta_start", ""), request.POST.get("eta_end", ""),
        as_cash=bool(request.POST.get("as_cash")),
    )


@login_required
@require_http_methods(["POST"])
def send_reschedule_msg_view(request):
    return _run(request, dispatch_queue.send_reschedule, request.POST.get("order_id"))


@login_required
@require_http_methods(["POST"])
def assign_driver_view(request):
    return _run(request, dispatch_queue.assign_driver, request.POST.get("order_id"), request.POST.get("driver_id"))


@login_required
@require_http_methods(["POST"])
def link_order_view(request):
    order_id = request.POST.get("order_id")
    primary_order_id = request.POST.get("primary_order_id")
    if not order_id or not primary_order_id:
        return _done(request, "Pick the order to link to.")
    return _run(request, dispatch_queue.link_orders, order_id, primary_order_id)


@login_required
@require_http_methods(["POST"])
def unlink_order_view(request):
    order_id = request.POST.get("order_id")
    if not order_id:
        return _done(request)
    return _run(request, dispatch_queue.unlink_order, order_id)


@login_required
@require_http_methods(["POST"])
def retry_task_view(request):
    """Re-queues the browser task that failed for an order. The card asks for a confirm first
    when the failure was after the irreversible click (it may already have gone through)."""
    return _run(request, dispatch_queue.retry_failed_task, request.POST.get("order_id"))


@login_required
@require_http_methods(["POST"])
def dismiss_error_view(request):
    return _run(request, dispatch_queue.dismiss_error, request.POST.get("order_id"))


@login_required
@require_http_methods(["POST"])
def clear_dispatch_queue_view(request):
    """Dismisses every order in the Dispatch Queue without texting anyone. The frontend
    confirms first, since real pending orders cleared this way never get their text."""
    dispatch_queue.clear_queue()
    return _done(request)


@login_required
@require_http_methods(["POST"])
def dismiss_notice_view(request):
    if not Notice.objects.filter(pk=_pk(request.POST.get("notice_id"))).update(dismissed=True):
        raise Http404("no such notice")
    return _done(request)


@login_required
@require_http_methods(["POST"])
def complete_order_manual(request):
    order_id = request.POST.get("order_id")
    if order_id:
        complete_order(order_id=order_id)
    return _done(request)


# ── Restock and invoices ─────────────────────────────────────────────────────

@login_required
@require_http_methods(["POST"])
def reconcile_restock(request):
    restock.reconcile(request.POST.getlist("order_ids"))
    return _done(request)


@login_required
@require_http_methods(["POST"])
def clear_restock(request):
    restock.clear_restock_list()
    return _done(request)


@login_required
@require_http_methods(["POST"])
def add_products_from_invoice(request):
    invoice_file = request.FILES.get("invoice")
    if invoice_file is None:
        return _done(request, "Choose an invoice file first.")
    try:
        check_upload(invoice_file.content_type, invoice_file.size)
        products = extract_products_from_invoice(invoice_file.read(), invoice_file.content_type)
    except InvoiceRejected as e:
        return _done(request, str(e))
    # creating products writes to WebJoint, so it is off in test mode
    if settings.RESTOCK_TEST_MODE:
        return _done(request, "Test mode is on: the products were read but not created in WebJoint.")
    tasks.create_product_task.delay(products)
    return _done(request)


# ── WebJoint webhooks (each served at hooks/<WEBHOOK_SECRET>/<name>) ──────────

def _json_body(request) -> dict:
    try:
        data = json.loads(request.body)
    except ValueError:
        raise InvalidPayload("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidPayload("body is not a JSON object")
    return data


def _webhook_order_id(data: dict) -> str:
    if data.get("id") in (None, ""):
        raise InvalidPayload("missing id")
    return str(data["id"])


def _rejected(view_name: str, error: InvalidPayload):
    logger.warning("%s rejected: %s", view_name, error)
    return JsonResponse({"status": "error", "detail": str(error)}, status=400)


@csrf_exempt
def legacy_webhook(request):
    """The old bare /new_order, /complete_order and /cancel_order paths. They no longer do
    anything (WebJoint must call hooks/<WEBHOOK_SECRET>/... instead) but stay routed so a stale
    registration shows up in the log. Exempt from CSRF only so a POST gets this logged 403
    rather than a bare CSRF failure."""
    logger.warning("webhook hit on retired path %s from %s", request.path, client_ip(request))
    return HttpResponseForbidden()


@csrf_exempt
@require_webhook_key
@require_http_methods(["POST"])
def new_order(request):
    try:
        intake.handle_new_order(_json_body(request))
    except InvalidPayload as e:
        return _rejected("new_order", e)
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_webhook_key
@require_http_methods(["POST"])
def complete_order_view(request):
    try:
        data = _json_body(request)
        order_id = _webhook_order_id(data)
    except InvalidPayload as e:
        return _rejected("complete_order", e)
    shipping = data.get("shipping")
    complete_order(order_id=order_id, city=(shipping.get("city") if isinstance(shipping, dict) else "") or "")
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_webhook_key
@require_http_methods(["POST"])
def cancel_order_view(request):
    try:
        order_id = _webhook_order_id(_json_body(request))
    except InvalidPayload as e:
        return _rejected("cancel_order", e)
    cancel_order(order_id=order_id)
    return JsonResponse({"status": "ok"})
