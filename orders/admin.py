from django.contrib import admin

from orders.models import City, CustomerFlag, SpecialAddress, TravelTime

# Business config: change it here, no deploy needed.


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ("name", "minimum")
    list_editable = ("minimum",)
    search_fields = ("name",)


@admin.register(CustomerFlag)
class CustomerFlagAdmin(admin.ModelAdmin):
    list_display = ("name", "merchant_pay", "no_text")
    list_editable = ("merchant_pay", "no_text")
    search_fields = ("name",)


@admin.register(SpecialAddress)
class SpecialAddressAdmin(admin.ModelAdmin):
    list_display = ("number", "street_contains", "city", "zip", "kind")
    list_display_links = ("number",)
    list_editable = ("street_contains", "city", "zip", "kind")


@admin.register(TravelTime)
class TravelTimeAdmin(admin.ModelAdmin):
    list_display = ("city_a", "city_b", "minutes", "source", "fetched_at")
    list_display_links = ("city_a",)
    list_editable = ("minutes", "source")
    list_filter = ("source",)
    search_fields = ("city_a", "city_b")
