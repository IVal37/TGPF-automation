# imports from std lib
import json
import logging
from datetime import datetime
from decimal import Decimal

# imports from django
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# imports from project
from django.conf import settings
from orders.constants import HOME_CITY, PaymentType
from orders.models import Driver, Notice, Order, OrderItem, OPEN_STATUSES, QUEUE_STATUSES, OrderStatus
from orders.services.dispatch import (
    build_dispatch_msg_from_order, build_reschedule_msg_from_order,
    extract_msg_info, get_dispatch_msg, is_fsa_fsm, is_merchant_pay_customer,
    is_under_min, is_weedmaps_source,
)
from orders.services.note import get_order_notes
from orders.tasks import (
    RETRYABLE_TASKS, create_product_task, fill_order_notes_task, prepare_order_task,
    resolve_payment_type_task, send_merchant_pay_link_task, send_message_task,
)
from orders.services.invoice import extract_products_from_invoice
from orders.services.time import parse_webhook_time
from orders.services import config
from orders.services.hours import is_past_closing
from orders.services.engine import (
    add_driver, add_order, add_shell_order, add_storefront_order,
    assign_order, cancel_order, complete_order, delete_driver, remove_driver,
)
from orders.services import queries
from orders.webhooks import client_ip, require_webhook_key

logger = logging.getLogger(__name__)

# orders with a real ETA window still waiting on a dispatcher to approve/send
def _pending_queryset():
    # queued orders with an ETA window to approve, plus today's unassigned orders
    # (no driver yet, so no ETA) waiting for a dispatcher to pick a driver
    queued = Q(status__in=QUEUE_STATUSES, eta_start__isnull=False)
    unassigned = Q(status__in=OPEN_STATUSES, driver__isnull=True, order_date__date=queries.today())
    # plus anything being texted right now, and anything with a failure to show
    # (even an order already sent: a note that failed to save still needs a look)
    sending = Q(status=OrderStatus.SENDING, order_date__date=queries.today())
    failed = ~Q(last_error="")
    return Order.objects.filter(queued | unassigned | sending | failed, is_shell=False)

# status is the source of truth; dispatch_sent is kept written for one release
# (rollback safety) and never read
def _mark_dispatch_sent(order_id: str) -> None:
    Order.objects.filter(order_id=order_id).update(status=OrderStatus.SENT, dispatch_sent=True)

# Texts the customer through the browser queue. The order goes to "sending"
# FIRST (no longer in the queue, so a second Send click is a 404 and can't text
# twice); send_message_task moves it to "sent" once the text is out, or leaves it
# on "sending" with the error shown on its card if it fails.
def _queue_text(order_id: str, phone: str, text: str) -> None:
    Order.objects.filter(order_id=order_id).update(status=OrderStatus.SENDING, dispatch_sent=True)
    send_message_task.delay(order_id, phone, text)

# queues a WebJoint Selenium task for exactly one order, identified by its
# WebJoint id. Orders saved before webjoint_id existed (or from a payload
# without _id) have none; acting on "some" order is exactly the bug this
# avoids, so those are skipped with a warning instead.
def _queue_webjoint(task, order_id: str, webjoint_id: str, *args) -> None:
    if not webjoint_id:
        logger.warning("no WebJoint id for order %s; skipped %s", order_id, task.name)
        return
    task.delay(webjoint_id, *args)

def _pending_orders():
    # unassigned cards first (they need a driver before anything else), then by ETA
    orders_list = sorted(
        _pending_queryset(),
        key=lambda o: (o.driver_id is not None, o.eta_start or o.order_date, o.pk),
    )
    result = []
    for order in orders_list:
        order_is_fsa_fsm = is_fsa_fsm(order.customer_name)
        unassigned = order.driver_id is None
        result.append({
            "order": order,
            "unassigned": unassigned,
            # waiting for a dispatcher (approve + send); other cards are only here
            # because they are sending right now or have an error to show
            "in_queue": order.status in QUEUE_STATUSES,
            "sending": order.status == OrderStatus.SENDING,
            # an unassigned order that was texted anyway (under-min, or its driver
            # was switched off after the dispatch text went out)
            "already_texted": unassigned and order.status in (OrderStatus.SENDING, OrderStatus.SENT),
            "eta_start_value": order.eta_start.strftime("%H:%M") if order.eta_start else "",
            "eta_end_value": order.eta_end.strftime("%H:%M") if order.eta_end else "",
            "past_closing": order.status in QUEUE_STATUSES and bool(order.eta_start) and is_past_closing(order.eta_start),
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
    return render(request, 'orders/_pending_orders.html', {
        'pending_orders': _pending_orders(),
        'active_drivers': list(queries.active_drivers()),
    })

# completed delivery orders not yet ticked under "Restock Time"
def _unreconciled_orders():
    return Order.objects.filter(status=OrderStatus.COMPLETED, is_shell=False, restock_reconciled=False)

# the restock tab: the orders waiting to be reconciled, and the list of what to
# restock (reconciled orders' items, summed, until "Complete Restock" clears them)
def _restock_context():
    rows = (
        OrderItem.objects
        .filter(order__restock_reconciled=True, order__restock_cleared=False)
        .values('product_name')
        .annotate(quantity=Sum('quantity'))
        .order_by('product_name')
    )
    return {
        'completed_orders': list(_unreconciled_orders().order_by('pk')),
        'restock_items': {row['product_name']: {'quantity': row['quantity']} for row in rows},
    }

@login_required
def restock_partial(request):
    """Renders just the completed-orders/restock-list content, polled by the
    dashboard's JS to auto-refresh that tab without a full page reload."""
    return render(request, 'orders/_restock.html', _restock_context())

@login_required
@require_http_methods(["POST"])
def clear_dispatch_queue_view(request):
    """Dismisses every order currently sitting in the Dispatch Queue without
    texting anyone or writing any WebJoint notes — a cleanup/testing utility,
    not a bulk-send. Real pending orders cleared this way never get their
    dispatch SMS, so the frontend confirms before submitting."""
    _pending_queryset().update(status=OrderStatus.SENT, dispatch_sent=True)
    return redirect("/")

# every driver (on shift or not) with what they are currently out delivering,
# for the Startup panel
def _driver_panel():
    return [
        {"driver": driver, "out_for_delivery": list(queries.out_for_delivery(driver.pk)) if driver.is_active else []}
        for driver in Driver.objects.order_by("pk")
    ]

@login_required
def order_list(request):
    pending_orders = _pending_orders()
    return render(request, 'orders/order_list.html', {
        **_restock_context(),
        'active_drivers': list(queries.active_drivers()),
        'drivers': _driver_panel(),
        'notices': Notice.objects.filter(dismissed=False).order_by('-created_at')[:10],
        'cities': config.cities(),
        'pending_orders': pending_orders,
    })

@login_required
@require_http_methods(["POST"])
def add_driver_view(request):
    """Creates a driver (starting the day in Davis) and puts them on shift."""
    name = request.POST.get("name", "").strip()[:255]
    if name:
        driver = Driver.objects.create(name=name)
        add_driver(driver.pk)
        driver.set_current_city(HOME_CITY)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def toggle_driver_view(request):
    """Switches a driver on or off shift. Switching one off unassigns their open orders."""
    driver = get_object_or_404(Driver, pk=request.POST.get("driver_id"))
    if driver.is_active:
        remove_driver(driver.pk)
    else:
        add_driver(driver.pk)
    return redirect("/")

def _clear_error(order: Order) -> None:
    Order.objects.filter(pk=order.pk).update(
        last_error="", last_error_at=None, last_error_task="", last_error_args={}, last_error_after_commit=False,
    )

@login_required
@require_http_methods(["POST"])
def retry_task_view(request):
    """Re-queues the browser task that failed for an order, with the same
    arguments, and clears the error. The card asks for a confirm first when the
    failure was after the irreversible click (it may already have gone through)."""
    order = get_object_or_404(Order, order_id=request.POST.get("order_id"), last_error__gt="")
    task = RETRYABLE_TASKS.get(order.last_error_task)
    if task is None:
        raise Http404("no retryable task recorded for this order")
    payload = order.last_error_args or {}
    _clear_error(order)
    # like every other browser task, only queued outside test mode
    if not settings.RESTOCK_TEST_MODE:
        task.delay(*payload.get("args", []), **payload.get("kwargs", {}))
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def dismiss_error_view(request):
    """Clears an order's failure. For an order stuck on "sending" (with or without
    an error: a worker that died mid-send leaves none) the dispatcher is saying
    they handled the text themselves, so it becomes "sent"."""
    order = get_object_or_404(
        Order, Q(last_error__gt="") | Q(status=OrderStatus.SENDING), order_id=request.POST.get("order_id"),
    )
    _clear_error(order)
    if order.status == OrderStatus.SENDING:
        _mark_dispatch_sent(order.order_id)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def dismiss_notice_view(request):
    get_object_or_404(Notice, pk=request.POST.get("notice_id"))
    Notice.objects.filter(pk=request.POST.get("notice_id")).update(dismissed=True)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def assign_driver_view(request):
    """Hands an unassigned order to a driver. A queued order gets its ETA now and
    becomes a normal Dispatch Queue card; one that was already texted just gets
    the driver (the customer keeps the ETA they were told)."""
    order = get_object_or_404(
        Order, order_id=request.POST.get("order_id"),
        status__in=OPEN_STATUSES, is_shell=False, driver__isnull=True,
    )
    driver = get_object_or_404(Driver, pk=request.POST.get("driver_id"), is_active=True)
    was_queued = order.status in QUEUE_STATUSES

    assign_order(order.order_id, driver.pk)

    if was_queued and config.is_no_text_customer(order.customer_name) and not settings.RESTOCK_TEST_MODE:
        # new_order skipped this customer's notes while there was no driver/ETA
        fresh = Order.objects.get(order_id=order.order_id)
        if fresh.payment_type:
            _queue_webjoint(fill_order_notes_task, fresh.order_id, fresh.webjoint_id, get_order_notes(fresh))
        else:
            # the payment lookup is still running; its task writes the note once it lands
            resolve_payment_type_task.delay(fresh.order_id, write_notes=True)
        _mark_dispatch_sent(fresh.order_id)
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
    # special addresses live in the database (SpecialAddress, edited in admin): a
    # "blocked" one means don't create an order at all; the "storefront" (3rd St) is
    # a real sale that still needs tracking for the storefront restock digest, so it
    # gets its own routing below (it always wins over blocked)
    special_address = config.match_special_address(data.get("shipping", {}))
    is_third_st = special_address == "storefront"
    if special_address == "blocked":
        return JsonResponse({"status": "ok"})

    # WebJoint resends a webhook it thinks failed (e.g. after a timeout); the
    # order is already saved and handled, so answer 200 and do nothing more
    if Order.objects.filter(order_id=str(data["id"])).exists():
        logger.info("duplicate webhook for order %s; ignored", data["id"])
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

    # WebJoint's own key for this order; every WebJoint action below targets it
    webjoint_id = data.get("_id", "")
    if not webjoint_id:
        logger.warning("new_order %s: webhook has no _id; WebJoint actions will be skipped", order_fields["order_id"])

    # WebJoint's webhook always labels Weedmaps orders "Cash", so their real
    # payment type is looked up on Weedmaps by a background task (below); until
    # it lands the order's payment_type stays empty and the dashboard holds Send.
    # In test mode nothing is scraped and the webhook's label is kept.
    resolve_payment = False
    weedmaps_order_id = ""
    if is_weedmaps_source(data.get("source", "")) and not settings.RESTOCK_TEST_MODE:
        resolve_payment = True
        weedmaps_order_id = str(data.get("weedmapsOrderId") or "")

    # a scheduled order keeps the window the customer picked, so it can still be
    # applied if the order is assigned to a driver later
    cart = data.get("cart") or {}
    scheduled = cart.get("deliveryTiming") == "Scheduled" and cart.get("scheduledStart") and cart.get("scheduledEnd")

    order = add_order(
        order_city=data["shipping"]["city"],
        customer_phone=data["customer"]["phone"][1:],
        webjoint_id=webjoint_id,
        weedmaps_order_id=weedmaps_order_id,
        status=OrderStatus.AWAITING_PAYMENT if resolve_payment else OrderStatus.AWAITING_APPROVAL,
        scheduled_start=parse_webhook_time(cart["scheduledStart"]) if scheduled else None,
        scheduled_end=parse_webhook_time(cart["scheduledEnd"]) if scheduled else None,
        **order_fields,
    )
    # nobody was on shift: the order is saved without a driver and waits in the
    # Dispatch Queue as an Unassigned card (see assign_driver_view)
    unassigned = order.driver_id is None
    msg_dict = extract_msg_info(data)
    if resolve_payment:
        msg_dict["pay_type"] = ""
    no_text = config.is_no_text_customer(data["customer"]["name"])
    under_min = is_under_min(msg_dict)
    # sets order.eta_start/eta_end via get_eta (including any scheduled-order
    # override) and routes under-min vs normal internally. For normal (has-ETA)
    # orders the returned text is discarded below — the real send happens later
    # from send_dispatch_msg_view, once a dispatcher has approved or edited the
    # ETA window. An unassigned order has no route to estimate from, so it skips
    # this; the under-min text needs no ETA, so that one is still built.
    dispatch_msg = None if unassigned and not under_min else get_dispatch_msg(msg_dict)
    Order.objects.filter(order_id=msg_dict["id"]).update(payment_type=msg_dict["pay_type"])

    if not settings.RESTOCK_TEST_MODE:
        if under_min:
            # no ETA window in this message, nothing to double-check — auto-send
            if no_text:
                _mark_dispatch_sent(msg_dict["id"])
            else:
                _queue_text(msg_dict["id"], msg_dict["phone"], dispatch_msg)
            _queue_webjoint(fill_order_notes_task, msg_dict["id"], webjoint_id, "reached out about under min")
        elif no_text and not unassigned:
            # excluded customer: we never text them, so there's nothing to hold
            # for approval — write notes with the system-computed window now.
            # For a Weedmaps order the note depends on the payment type, which
            # isn't known yet, so resolve_payment_type_task writes it instead.
            if not resolve_payment:
                order = Order.objects.get(order_id=msg_dict["id"])
                _queue_webjoint(fill_order_notes_task, msg_dict["id"], webjoint_id, get_order_notes(order))
            _mark_dispatch_sent(msg_dict["id"])
        # else: normal dispatch with a real ETA window — leave it awaiting approval
        # so it shows up in the Dispatch Queue tab for a human to approve/edit
        # and send (see send_dispatch_msg_view / send_reschedule_msg_view below).

        if is_merchant_pay_customer(msg_dict["name"], msg_dict["last_name"]) and not under_min:
            send_merchant_pay_link_task.delay(msg_dict["id"])

        # first in the browser queue, since a dispatcher is waiting on it
        if resolve_payment:
            resolve_payment_type_task.delay(msg_dict["id"], write_notes=no_text and not under_min and not unassigned)
        # pull the order's items, and dispatch it if it's still Pending
        _queue_webjoint(prepare_order_task, msg_dict["id"], webjoint_id, data.get("status") == "Pending")
    return JsonResponse({"status": "ok"})

@login_required
@require_http_methods(["POST"])
def send_dispatch_msg_view(request):
    order_id = request.POST.get("order_id")
    eta_start_str = request.POST.get("eta_start")
    eta_end_str = request.POST.get("eta_end")
    order = get_object_or_404(Order, order_id=order_id, status__in=QUEUE_STATUSES)

    if order.driver_id is None:
        # no driver means no ETA window to send
        logger.warning("send_dispatch_msg: order %s has no driver yet; not sent", order_id)
        return redirect("/")

    if not order.payment_type:
        # the payment type changes the price in the text; sending before it's
        # known would quote the wrong total
        if not request.POST.get("as_cash"):
            logger.warning("send_dispatch_msg: order %s has no payment type yet; not sent", order_id)
            return redirect("/")
        order.payment_type = PaymentType.CASH
        order.save()
    elif request.POST.get("as_cash"):
        # the lookup finished after this page loaded; show the real value first
        logger.warning("send_dispatch_msg: order %s now has payment type %s; \"Send as cash\" ignored",
                       order_id, order.payment_type)
        return redirect("/")

    if eta_start_str and eta_end_str:
        base_date = order.eta_start.date() if order.eta_start else datetime.now().date()
        eta_start = datetime.combine(base_date, datetime.strptime(eta_start_str, "%H:%M").time())
        eta_end = datetime.combine(base_date, datetime.strptime(eta_end_str, "%H:%M").time())
        order.set_etas(eta_start, eta_end)

    if settings.RESTOCK_TEST_MODE:
        _mark_dispatch_sent(order_id)
    else:
        _queue_text(order_id, order.customer_phone, build_dispatch_msg_from_order(order))
        _queue_webjoint(fill_order_notes_task, order.order_id, order.webjoint_id, get_order_notes(order))
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def send_reschedule_msg_view(request):
    order_id = request.POST.get("order_id")
    order = get_object_or_404(Order, order_id=order_id, status__in=QUEUE_STATUSES)

    if settings.RESTOCK_TEST_MODE:
        _mark_dispatch_sent(order_id)
    else:
        _queue_text(order_id, order.customer_phone, build_reschedule_msg_from_order(order))
        _queue_webjoint(
            fill_order_notes_task, order.order_id, order.webjoint_id,
            "reached out about rescheduling for tomorrow morning",
        )
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

    companion = get_object_or_404(Order, order_id=order_id, status__in=QUEUE_STATUSES)
    primary = get_object_or_404(Order, order_id=primary_order_id, status__in=QUEUE_STATUSES)

    if not is_fsa_fsm(companion.customer_name):
        return redirect("/")

    companion.linked_order = primary
    companion.status = OrderStatus.SENT
    companion.dispatch_sent = True  # kept in step with status for one release; never read
    companion.save()

    if not settings.RESTOCK_TEST_MODE:
        # the note goes on the companion's own WebJoint order, not the primary's
        _queue_webjoint(
            fill_order_notes_task, companion.order_id, companion.webjoint_id,
            f"second part of {primary.customer_name}'s order",
        )

    return redirect("/")

@login_required
@require_http_methods(["POST"])
def unlink_order_view(request):
    """Reverses link_order_view — puts the FSA/FSM order back into the
    Dispatch Queue for normal handling."""
    order_id = request.POST.get("order_id")
    if not order_id:
        return redirect("/")

    companion = get_object_or_404(Order, order_id=order_id, status__in=OPEN_STATUSES)
    companion.linked_order = None
    # back in the queue; Send stays held if its payment type was never filled in
    companion.status = OrderStatus.AWAITING_APPROVAL if companion.payment_type else OrderStatus.AWAITING_PAYMENT
    companion.dispatch_sent = False  # kept in step with status for one release; never read
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
    selected_ids = request.POST.getlist("order_ids")
    _unreconciled_orders().filter(order_id__in=selected_ids).update(restock_reconciled=True)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def clear_restock(request):
    Order.objects.filter(restock_reconciled=True, restock_cleared=False).update(restock_cleared=True)
    return redirect("/")

@login_required
@require_http_methods(["POST"])
def add_shell_order_view(request):
    city = request.POST.get("city")
    if city:
        try:
            add_shell_order(city)
        except ValueError:
            logger.warning("add_shell_order: no driver on shift; shell for %s not added", city)
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
        get_object_or_404(Driver, pk=int(driver_id), is_active=True).set_current_city(city)
    return redirect("/")
