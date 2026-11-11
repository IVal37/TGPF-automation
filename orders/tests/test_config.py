"""Phase 7a: business config lives in the database, travel times can come from Google.

The OLD_* values below are a verbatim copy of what orders/constants.py used to hard-code.
They are the reference the seeded database must reproduce exactly.
"""
import importlib
import io
from datetime import timedelta
from decimal import Decimal
from itertools import product
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from orders.models import City, CustomerFlag, SpecialAddress, TravelTime
from orders.services import config, travel

OLD_CITY_MINS = {
    "American Canyon": Decimal("125.00"), "Brooks": Decimal("200.00"), "Davis": Decimal("25.00"),
    "Dixon": Decimal("35.00"), "Dunnigan": Decimal("200.00"), "East Sacramento": Decimal("75.00"),
    "Elk Grove": Decimal("65.00"), "Esparto": Decimal("100.00"), "Fairfield": Decimal("55.00"),
    "Guinda": Decimal("250.00"), "Knights Landing": Decimal("125.00"), "Midtown Sacramento": Decimal("65.00"),
    "Roseville": Decimal("175.00"), "Rocklin": Decimal("175.00"), "South Sacramento": Decimal("100.00"),
    "Suisun City": Decimal("60.00"), "Vacaville": Decimal("45.00"), "Vallejo": Decimal("125.00"),
    "West Sacramento": Decimal("55.00"), "Winters": Decimal("55.00"), "Woodland": Decimal("35.00"),
}
OLD_MERCHANT_PAY_CUSTOMERS = ("Imran Rahim", "Emmi Towns", "Chelsea Rosenkild")
OLD_TRAVEL_BASE = {
    ("Davis", "Dixon"): 15, ("Davis", "Fairfield"): 40, ("Davis", "Vacaville"): 35,
    ("Davis", "Winters"): 30, ("Davis", "Woodland"): 20,
    ("Dixon", "Fairfield"): 25, ("Dixon", "Vacaville"): 20, ("Dixon", "Winters"): 20, ("Dixon", "Woodland"): 25,
    ("Fairfield", "Vacaville"): 15, ("Fairfield", "Winters"): 25, ("Fairfield", "Woodland"): 45,
    ("Vacaville", "Winters"): 25, ("Vacaville", "Woodland"): 30, ("Winters", "Woodland"): 30,
}


def old_get_travel_time(a, b):
    """The old constants.get_travel_time, for comparison."""
    if a == b:
        return 15
    return OLD_TRAVEL_BASE.get((a, b), OLD_TRAVEL_BASE.get((b, a), 30))


def old_is_third_st(shipping):
    street = shipping.get("street", "").strip().lower()
    return (
        shipping.get("number", "").strip() == "526"
        and shipping.get("city", "").strip() == "Davis"
        and any(w in street for w in ("third", "3rd"))
    )


def old_is_blocked(shipping):
    full_street = f"{shipping.get('number', '').strip()} {shipping.get('street', '').strip()}".strip()
    return (full_street, shipping.get("zip", "").strip()) in {("526 3rd Street", "95616")} and not old_is_third_st(shipping)


class TestSeededConfigMatchesTheOldConstants(TestCase):
    def test_every_city_minimum(self):
        self.assertEqual(len(OLD_CITY_MINS), 21)
        for city, minimum in OLD_CITY_MINS.items():
            self.assertEqual(config.city_min(city), minimum, city)

    def test_no_extra_or_missing_cities(self):
        self.assertEqual(config.cities(), sorted(OLD_CITY_MINS))

    def test_an_unknown_city_has_no_minimum(self):
        self.assertEqual(config.city_min("Atlantis"), Decimal("0.00"))

    def test_merchant_pay_customers(self):
        for full in OLD_MERCHANT_PAY_CUSTOMERS:
            first, last = full.split()
            self.assertTrue(config.is_merchant_pay_customer(first, last), full)
        for first, last in (("Imran", ""), ("Imran", "Rahimi"), ("Hailey", "White"), ("", "")):
            self.assertFalse(config.is_merchant_pay_customer(first, last), (first, last))

    def test_no_text_customer(self):
        self.assertTrue(config.is_no_text_customer("Mohsen Awara"))
        self.assertTrue(config.is_no_text_customer("  mohsen   awara "))  # old: .strip().lower()
        self.assertFalse(config.is_no_text_customer("Mohsen"))
        self.assertFalse(config.is_no_text_customer("Hailey White"))
        self.assertFalse(config.is_no_text_customer(""))

    def test_merchant_pay_and_no_text_flags_do_not_bleed_into_each_other(self):
        self.assertFalse(config.is_no_text_customer("Imran Rahim"))
        self.assertFalse(config.is_merchant_pay_customer("Mohsen", "Awara"))

    def test_travel_time_for_every_ordered_pair(self):
        cities = sorted(OLD_CITY_MINS) + ["Atlantis"]
        for a, b in product(cities, cities):
            self.assertEqual(travel.get_travel_time(a, b), old_get_travel_time(a, b), (a, b))

    def test_all_15_old_pairs_are_seeded_as_static(self):
        self.assertEqual(TravelTime.objects.count(), 15)
        self.assertFalse(TravelTime.objects.exclude(source="static").exists())
        self.assertFalse(TravelTime.objects.exclude(fetched_at=None).exists())

    def test_the_pairs_are_stored_once_in_alphabetical_order(self):
        for row in TravelTime.objects.all():
            self.assertLess(row.city_a, row.city_b)

    def test_no_request_is_made_without_a_key(self):
        with patch.object(travel.requests, "get") as get:
            travel.get_travel_time("Davis", "Fairfield")
            travel.get_travel_time("Rocklin", "Elk Grove")
        get.assert_not_called()


class TestMatchSpecialAddress(TestCase):
    def test_the_existing_third_st_cases(self):
        cases = [
            ({"city": "Davis", "number": "526", "street": "3rd Street", "zip": "95616"}, "storefront"),
            ({"city": "Davis", "number": "526", "street": "Third St", "zip": "95616"}, "storefront"),
            ({"city": "Davis", "number": "526", "street": "3rd St"}, "storefront"),
            ({"city": "Davis", "number": " 526 ", "street": " THIRD Street "}, "storefront"),
            ({"city": "Davis", "number": "527", "street": "3rd Street", "zip": "95616"}, None),
            ({"city": "Davis", "number": "526", "street": "Main Street", "zip": "95616"}, None),
            ({"city": "Woodland", "number": "526", "street": "3rd St", "zip": "95695"}, None),
            ({"city": "Davis"}, None),
            ({}, None),
        ]
        for shipping, expected in cases:
            self.assertEqual(config.match_special_address(shipping), expected, shipping)

    def test_the_old_blocked_entry_in_another_city_is_still_blocked(self):
        shipping = {"city": "Woodland", "number": "526", "street": "3rd Street", "zip": "95616"}
        self.assertEqual(config.match_special_address(shipping), "blocked")

    def test_storefront_wins_over_blocked(self):
        shipping = {"city": "Davis", "number": "526", "street": "3rd Street", "zip": "95616"}
        self.assertEqual(config.match_special_address(shipping), "storefront")

    def test_agrees_with_the_old_hardcoded_checks_across_a_grid_of_addresses(self):
        numbers = ["526", "527", " 526 ", ""]
        streets = ["3rd Street", "Third St", "3rd", "Main Street", ""]
        cities = ["Davis", "Woodland", ""]
        zips = ["95616", "95695", ""]
        for number, street, city, zip_code in product(numbers, streets, cities, zips):
            shipping = {"number": number, "street": street, "city": city, "zip": zip_code}
            expected = "storefront" if old_is_third_st(shipping) else ("blocked" if old_is_blocked(shipping) else None)
            self.assertEqual(config.match_special_address(shipping), expected, shipping)

    def test_the_known_differences_from_the_old_exact_match(self):
        # The old blocked check compared the whole "<number> <street>" text exactly and
        # case-sensitively. The new rule is "street contains ..., any case", so a few
        # unusual spellings of the blocked address are now blocked too (a repeated number,
        # different capitals, an apartment suffix). Those never differ for the real
        # address as WebJoint sends it, and blocking them is the safer reading.
        for street in ("526 3rd Street", "3RD STREET", "3rd Street Apt 4"):
            shipping = {"number": "526", "street": street, "city": "Woodland", "zip": "95616"}
            self.assertFalse(old_is_blocked(shipping), street)
            self.assertEqual(config.match_special_address(shipping), "blocked", street)

    def test_an_address_added_in_admin_blocks_orders(self):
        SpecialAddress.objects.create(number="12", street_contains="elm", city="", zip="", kind="blocked")
        self.assertEqual(config.match_special_address({"number": "12", "street": "Elm Court", "city": "Dixon"}), "blocked")
        self.assertIsNone(config.match_special_address({"number": "13", "street": "Elm Court", "city": "Dixon"}))

    def test_blank_city_and_zip_match_anything_but_a_set_one_must_match(self):
        SpecialAddress.objects.create(number="9", street_contains="oak", city="Dixon", zip="95620", kind="blocked")
        self.assertEqual(config.match_special_address({"number": "9", "street": "Oak", "city": "dixon", "zip": "95620"}), "blocked")
        self.assertIsNone(config.match_special_address({"number": "9", "street": "Oak", "city": "Davis", "zip": "95620"}))
        self.assertIsNone(config.match_special_address({"number": "9", "street": "Oak", "city": "Dixon", "zip": "11111"}))


class TestNewOrderUsesTheConfig(TestCase):
    """new_order against the real config (only the engine and tasks are mocked)."""

    def _post(self, **shipping):
        import json

        from django.conf import settings

        from orders.tests.helpers import POS_ORDER
        from orders.views import new_order
        payload = {**POS_ORDER, "shipping": {"city": "Davis", **shipping}}
        request = RequestFactory().post("/new_order", data=json.dumps(payload), content_type="application/json")
        with patch("orders.services.intake.add_order") as add_order, \
             patch("orders.services.intake.add_storefront_order") as storefront, \
             patch("orders.services.intake.under_min_text", return_value="msg"), \
             patch("orders.services.intake.is_under_min", return_value=True), \
             patch("orders.tasks.send_message_task"), patch("orders.tasks.fill_order_notes_task"), \
             patch("orders.tasks.prepare_order_task"):
            response = new_order(request, key=settings.WEBHOOK_SECRET)
        return response, add_order, storefront

    def test_storefront_address_is_routed_to_the_storefront(self):
        response, add_order, storefront = self._post(number="526", street="3rd Street", zip="95616")
        storefront.assert_called_once()
        add_order.assert_not_called()

    def test_blocked_address_creates_nothing(self):
        SpecialAddress.objects.create(number="12", street_contains="elm", kind="blocked")
        response, add_order, storefront = self._post(number="12", street="Elm Court")
        self.assertEqual(response.status_code, 200)
        add_order.assert_not_called()
        storefront.assert_not_called()

    def test_an_ordinary_address_is_a_normal_order(self):
        response, add_order, storefront = self._post(number="1", street="Main Street", zip="95616")
        add_order.assert_called_once()
        storefront.assert_not_called()

    def test_a_city_minimum_edited_in_the_database_takes_effect_immediately(self):
        from orders.services.pricing import is_under_min
        self.assertFalse(is_under_min(Decimal("60.00"), Decimal("0"), "Davis"))
        City.objects.filter(name="Davis").update(minimum=Decimal("100.00"))
        self.assertTrue(is_under_min(Decimal("60.00"), Decimal("0"), "Davis"))

    def test_the_dashboard_city_dropdowns_come_from_the_database(self):
        City.objects.create(name="Zzyzx", minimum=Decimal("10.00"))
        client = Client()
        client.force_login(User.objects.create_user("dispatcher"))
        html = client.get("/").content.decode()
        self.assertIn('<option value="Zzyzx">Zzyzx</option>', html)
        City.objects.filter(name="Davis").delete()
        self.assertNotIn('<option value="Davis">Davis</option>', client.get("/").content.decode())


def _google_response(minutes=None, *, element_status="OK", status="OK", http_error=None, json_error=None):
    response = MagicMock()
    if http_error:
        response.raise_for_status.side_effect = http_error
    if json_error:
        response.json.side_effect = json_error
    else:
        element = {"status": element_status}
        if minutes is not None:
            element["duration"] = {"value": int(minutes * 60), "text": f"{minutes} mins"}
        response.json.return_value = {"status": status, "rows": [{"elements": [element]}]}
    return response


@override_settings(GOOGLE_MAPS_API_KEY="test-key-123")
class TestGoogleTravelTimes(TestCase):
    def setUp(self):
        travel.reset_failure_memo()
        self.addCleanup(travel.reset_failure_memo)

    def _get(self, *responses_or_errors, a="Davis", b="Fairfield"):
        with patch.object(travel.requests, "get", side_effect=list(responses_or_errors)) as get:
            return travel.get_travel_time(a, b), get

    def test_a_good_answer_is_stored_and_returned(self):
        minutes, get = self._get(_google_response(37.4))
        self.assertEqual(minutes, 37)
        row = TravelTime.objects.get(city_a="Davis", city_b="Fairfield")
        self.assertEqual((row.minutes, row.source), (37, "google"))
        self.assertIsNotNone(row.fetched_at)
        self.assertEqual(TravelTime.objects.filter(city_a="Davis", city_b="Fairfield").count(), 1)

    def test_the_request_asks_for_california_cities_with_a_3_second_timeout(self):
        _, get = self._get(_google_response(37))
        args, kwargs = get.call_args
        self.assertEqual(args[0], travel.API_URL)
        self.assertEqual(kwargs["timeout"], 3)
        self.assertEqual(kwargs["params"]["origins"], "Davis, CA")
        self.assertEqual(kwargs["params"]["destinations"], "Fairfield, CA")
        self.assertEqual(kwargs["params"]["mode"], "driving")
        self.assertEqual(kwargs["params"]["key"], "test-key-123")

    def test_either_direction_uses_the_same_stored_row(self):
        self._get(_google_response(37))
        with patch.object(travel.requests, "get") as get:
            self.assertEqual(travel.get_travel_time("Fairfield", "Davis"), 37)
            self.assertEqual(travel.get_travel_time("Davis", "Fairfield"), 37)
        get.assert_not_called()

    def test_a_pair_with_no_row_is_fetched_and_stored(self):
        minutes, _ = self._get(_google_response(52), a="Rocklin", b="Elk Grove")
        self.assertEqual(minutes, 52)
        self.assertTrue(TravelTime.objects.filter(city_a="Elk Grove", city_b="Rocklin", source="google").exists())

    def test_same_city_never_calls_google(self):
        with patch.object(travel.requests, "get") as get:
            self.assertEqual(travel.get_travel_time("Davis", "Davis"), 15)
        get.assert_not_called()

    def test_fresh_google_row_makes_no_call(self):
        TravelTime.objects.filter(city_a="Davis", city_b="Fairfield").update(
            minutes=44, source="google", fetched_at=timezone.now() - timedelta(days=29))
        with patch.object(travel.requests, "get") as get:
            self.assertEqual(travel.get_travel_time("Davis", "Fairfield"), 44)
        get.assert_not_called()

    def test_stale_google_row_is_refreshed(self):
        TravelTime.objects.filter(city_a="Davis", city_b="Fairfield").update(
            minutes=44, source="google", fetched_at=timezone.now() - timedelta(days=31))
        minutes, get = self._get(_google_response(46))
        self.assertEqual(minutes, 46)
        self.assertEqual(TravelTime.objects.get(city_a="Davis", city_b="Fairfield").minutes, 46)

    def test_stale_row_is_kept_when_the_refresh_fails(self):
        TravelTime.objects.filter(city_a="Davis", city_b="Fairfield").update(
            minutes=44, source="google", fetched_at=timezone.now() - timedelta(days=31))
        with self.assertLogs("orders.services.travel", "WARNING"):
            minutes, _ = self._get(requests.Timeout("slow"))
        self.assertEqual(minutes, 44)
        self.assertEqual(TravelTime.objects.get(city_a="Davis", city_b="Fairfield").minutes, 44)

    def test_each_kind_of_error_falls_back_to_the_old_static_number(self):
        failures = {
            "timeout": requests.Timeout("slow"),
            "connection": requests.ConnectionError("no network"),
            "http error": _google_response(http_error=requests.HTTPError("500")),
            "bad json": _google_response(json_error=ValueError("not json")),
            "request denied": _google_response(status="REQUEST_DENIED"),
            "over limit": _google_response(status="OVER_QUERY_LIMIT"),
            "no route": _google_response(element_status="ZERO_RESULTS"),
            "unexpected shape": MagicMock(**{"json.return_value": {"status": "OK", "rows": []}}),
        }
        for label, failure in failures.items():
            travel.reset_failure_memo()
            TravelTime.objects.filter(city_a="Davis", city_b="Fairfield").update(minutes=40, source="static", fetched_at=None)
            with self.subTest(label), self.assertLogs("orders.services.travel", "WARNING"):
                minutes, _ = self._get(failure)
            self.assertEqual(minutes, old_get_travel_time("Davis", "Fairfield"), label)
            row = TravelTime.objects.get(city_a="Davis", city_b="Fairfield")
            self.assertEqual((row.source, row.fetched_at), ("static", None), label)  # nothing bad stored

    def test_an_unknown_pair_falls_back_to_30_when_google_fails(self):
        with self.assertLogs("orders.services.travel", "WARNING"):
            minutes, _ = self._get(requests.Timeout("slow"), a="Rocklin", b="Elk Grove")
        self.assertEqual(minutes, 30)
        self.assertFalse(TravelTime.objects.filter(city_a="Elk Grove").exists())

    def test_a_failed_pair_is_not_asked_again_for_a_while(self):
        with self.assertLogs("orders.services.travel", "WARNING"):
            _, get = self._get(requests.Timeout("slow"))
        with patch.object(travel.requests, "get") as get2:
            self.assertEqual(travel.get_travel_time("Davis", "Fairfield"), 40)
        get2.assert_not_called()
        self.assertEqual(get.call_count, 1)

    def test_the_api_key_never_appears_in_a_log_line(self):
        leaky = requests.ConnectionError("HTTPSConnectionPool: url: /maps/api/distancematrix/json?key=test-key-123&origins=Davis")
        with self.assertLogs("orders.services.travel", "WARNING") as logs:
            self._get(leaky)
        self.assertNotIn("test-key-123", "\n".join(logs.output))
        self.assertIn("***", "\n".join(logs.output))

    def test_minutes_are_rounded_and_never_zero(self):
        self.assertEqual(self._get(_google_response(37.6))[0], 38)
        travel.reset_failure_memo()
        TravelTime.objects.all().delete()
        response = _google_response(0.2)
        self.assertEqual(self._get(response)[0], 1)


class TestWithoutAKeyNothingChanges(TestCase):
    @override_settings(GOOGLE_MAPS_API_KEY="")
    def test_blank_key_never_calls_google_even_for_stale_rows(self):
        TravelTime.objects.update(fetched_at=timezone.now() - timedelta(days=90), source="google")
        with patch.object(travel.requests, "get") as get:
            self.assertEqual(travel.get_travel_time("Davis", "Dixon"), 15)
        get.assert_not_called()

    def test_the_eta_numbers_do_not_change(self):
        # the same scenario as TestGetEta: Davis -> Winters is 30 with no key
        self.assertEqual(travel.get_travel_time("Davis", "Winters"), 30)
        self.assertEqual(travel.get_travel_time("Winters", "Davis"), 30)


@override_settings(GOOGLE_MAPS_API_KEY="test-key-123")
class TestRefreshTravelTimesCommand(TestCase):
    def setUp(self):
        travel.reset_failure_memo()

    def _run(self, *args, response=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(travel.requests, "get", side_effect=response) as get:
            call_command("refresh_travel_times", *args, stdout=out, stderr=err)
        return get, out.getvalue(), err.getvalue()

    @staticmethod
    def _answer(minutes):
        def respond(url, params, timeout):
            destinations = params["destinations"].split("|")
            response = MagicMock()
            response.json.return_value = {"status": "OK", "rows": [{"elements": [
                {"status": "OK", "duration": {"value": minutes * 60}} for _ in destinations]}]}
            return response
        return respond

    def test_without_a_key_it_says_so_and_does_nothing(self):
        with override_settings(GOOGLE_MAPS_API_KEY=""):
            get, out, _ = self._run(response=self._answer(33))
        get.assert_not_called()
        self.assertIn("GOOGLE_MAPS_API_KEY is not set", out)

    def test_fetches_every_pair_one_request_per_origin_and_stores_them(self):
        get, out, _ = self._run(response=self._answer(33))
        self.assertEqual(get.call_count, 20)  # 21 cities: the last has nobody after it
        self.assertEqual(TravelTime.objects.count(), 210)  # 21 * 20 / 2
        self.assertFalse(TravelTime.objects.exclude(source="google").exists())
        self.assertFalse(TravelTime.objects.exclude(minutes=33).exists())
        self.assertIn("Updated 210 pair(s), 0 failed", out)
        for call in get.call_args_list:
            self.assertLessEqual(len(call.kwargs["params"]["destinations"].split("|")), travel.MAX_DESTINATIONS)
            self.assertEqual(call.kwargs["timeout"], 3)

    def test_a_second_run_only_fetches_what_is_missing_or_stale(self):
        self._run(response=self._answer(33))
        get, out, _ = self._run(response=self._answer(34))
        get.assert_not_called()
        self.assertIn("210 already fresh", out)

    def test_force_refetches_fresh_pairs(self):
        self._run(response=self._answer(33))
        get, _, _ = self._run("--force", response=self._answer(34))
        self.assertEqual(get.call_count, 20)
        self.assertFalse(TravelTime.objects.exclude(minutes=34).exists())

    def test_a_failed_request_is_reported_and_stores_nothing_for_it(self):
        get, out, err = self._run(response=requests.Timeout("slow"))
        self.assertIn("0 pair(s)", out)
        self.assertIn("210 failed", out)
        self.assertEqual(TravelTime.objects.filter(source="google").count(), 0)
        self.assertNotIn("test-key-123", err)
        # the seeded numbers still work
        self.assertEqual(travel.get_travel_time("Davis", "Fairfield"), 40)


class TestAdmin(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))

    def test_the_four_models_are_registered_with_list_editable(self):
        from django.contrib import admin
        for model in (City, CustomerFlag, SpecialAddress, TravelTime):
            model_admin = admin.site._registry[model]
            self.assertTrue(model_admin.list_editable, model.__name__)
            self.assertTrue(set(model_admin.list_editable) <= set(model_admin.list_display), model.__name__)
            self.assertFalse(set(model_admin.list_editable) & set(model_admin.get_list_display_links(None, model_admin.list_display)))

    def test_each_changelist_renders(self):
        for name in ("city", "customerflag", "specialaddress", "traveltime"):
            response = self.client.get(f"/admin/orders/{name}/")
            self.assertEqual(response.status_code, 200, name)

    def test_the_seeded_rows_show_up(self):
        html = self.client.get("/admin/orders/city/").content.decode()
        self.assertIn("Knights Landing", html)
        self.assertIn("Mohsen Awara", self.client.get("/admin/orders/customerflag/").content.decode())

    def test_a_city_minimum_can_be_edited_from_the_list(self):
        city = City.objects.get(name="Davis")
        rows = City.objects.order_by("name")
        data = {"form-TOTAL_FORMS": rows.count(), "form-INITIAL_FORMS": rows.count(), "form-MIN_NUM_FORMS": 0,
                "form-MAX_NUM_FORMS": 1000, "_save": "Save"}
        for i, row in enumerate(rows):
            data[f"form-{i}-id"] = row.pk
            data[f"form-{i}-minimum"] = "99.00" if row.pk == city.pk else str(row.minimum)
        self.client.post("/admin/orders/city/", data)
        city.refresh_from_db()
        self.assertEqual(city.minimum, Decimal("99.00"))

    def test_a_non_staff_user_cannot_use_the_admin(self):
        client = Client()
        client.force_login(User.objects.create_user("dispatcher"))
        self.assertEqual(client.get("/admin/orders/city/").status_code, 302)


class TestSeedMigration(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_seed_is_idempotent_and_survives_a_roll_back_and_forward(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("orders", "0014_visible_failures")])
        executor = MigrationExecutor(connection)
        executor.migrate([("orders", "0016_seed_business_config")])
        self.assertEqual((City.objects.count(), CustomerFlag.objects.count(),
                          SpecialAddress.objects.count(), TravelTime.objects.count()), (21, 4, 3, 15))
        # running the seed again changes nothing
        seed = importlib.import_module("orders.migrations.0016_seed_business_config").seed
        seed(executor.loader.project_state([("orders", "0016_seed_business_config")]).apps, None)
        self.assertEqual((City.objects.count(), CustomerFlag.objects.count(),
                          SpecialAddress.objects.count(), TravelTime.objects.count()), (21, 4, 3, 15))

    def test_hand_edited_values_are_not_reset_by_a_second_run_of_unrelated_rows(self):
        City.objects.filter(name="Davis").update(minimum=Decimal("99.00"))
        TravelTime.objects.filter(city_a="Davis", city_b="Dixon").update(minutes=22)
        # new rows an admin added are left alone
        City.objects.create(name="Zzyzx", minimum=Decimal("10.00"))
        self.assertTrue(City.objects.filter(name="Zzyzx").exists())
