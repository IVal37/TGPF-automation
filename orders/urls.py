# imports from django
from django.urls import path

# imports from project
from orders import views

urlpatterns = [
    path("", views.order_list, name="order_list"),
    # WebJoint webhooks live under a secret path segment (WEBHOOK_SECRET); the
    # old bare paths just answer 403 so a stale registration is easy to spot
    path("hooks/<str:key>/new_order", views.new_order, name="new_order"),
    path("hooks/<str:key>/complete_order", views.complete_order_view, name="complete_order_view"),
    path("hooks/<str:key>/cancel_order", views.cancel_order_view, name="cancel_order_view"),
    path("new_order", views.legacy_webhook),
    path("complete_order", views.legacy_webhook),
    path("cancel_order", views.legacy_webhook),
    path("add_driver", views.add_driver_view, name="add_driver_view"),
    path("toggle_driver", views.toggle_driver_view, name="toggle_driver_view"),
    path("assign_driver", views.assign_driver_view, name="assign_driver_view"),
    path("retry_task", views.retry_task_view, name="retry_task_view"),
    path("dismiss_error", views.dismiss_error_view, name="dismiss_error_view"),
    path("dismiss_notice", views.dismiss_notice_view, name="dismiss_notice_view"),
    path("delete_driver", views.delete_driver_view, name="delete_driver_view"),
    path("complete_order_manual", views.complete_order_manual, name="complete_order_manual"),
    path("reconcile_restock", views.reconcile_restock, name="reconcile_restock"),
    path("clear_restock", views.clear_restock, name="clear_restock"),
    path("add_shell_order", views.add_shell_order_view, name="add_shell_order_view"),
    path("set_driver_location", views.set_driver_location_view, name="set_driver_location_view"),
    path("add_products_from_invoice", views.add_products_from_invoice, name="add_products_from_invoice"),
    path("send_dispatch_msg", views.send_dispatch_msg_view, name="send_dispatch_msg_view"),
    path("send_reschedule_msg", views.send_reschedule_msg_view, name="send_reschedule_msg_view"),
    path("link_order", views.link_order_view, name="link_order_view"),
    path("unlink_order", views.unlink_order_view, name="unlink_order_view"),
    path("pending_orders_partial", views.pending_orders_partial, name="pending_orders_partial"),
    path("restock_partial", views.restock_partial, name="restock_partial"),
    path("clear_dispatch_queue", views.clear_dispatch_queue_view, name="clear_dispatch_queue_view"),
]
