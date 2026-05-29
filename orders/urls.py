# imports from django
from django.urls import path

# imports from project
from orders import views

urlpatterns = [
    path("", views.order_list),
    path("new_order", views.new_order),
    path("complete_order", views.complete_order_view),
    path("add_dummy_driver", views.add_dummy_driver),
    path("delete_driver", views.delete_driver_view),
    path("complete_order_manual", views.complete_order_manual),
]
