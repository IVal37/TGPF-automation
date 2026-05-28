# imports from django
from django.urls import path

# imports from project
from orders import views

urlpatterns = [
    path("new_order", views.new_order),
    path("complete_order", views.complete_order_view),
]
