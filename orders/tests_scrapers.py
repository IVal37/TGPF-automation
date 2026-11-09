"""Golden-trace tests for the Selenium scrapers.

Each scraper function is run against a fake Chrome whose elements record every
lookup and interaction, with every `time.sleep` recorded instead of slept. The
resulting trace (browser actions, sleeps, logged messages, return value) is
compared to a golden JSON file in orders/fixtures/scraper_traces/.

After an intentional scraper change, rewrite the goldens with:

    UPDATE_TRACES=1 python manage.py test orders.tests_scrapers --settings=orders.test_settings

and review/explain every diff before accepting it.
"""
import difflib
import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from orders.services.scrapers import merchant_pay, talkroute, webjoint, weedmaps
from orders.services.scrapers.browser import ci_contains

TRACE_DIR = Path(__file__).parent / "fixtures" / "scraper_traces"

# fake credentials so the real .env values never end up in a golden file
FAKE_ENV = {
    f"{site}_{field}": f"fake-{site.lower()}-{field.lower()}"
    for site in ("WEBJOINT", "TALKROUTE", "WEEDMAPS", "MERCHANT_PAY")
    for field in ("EMAIL", "PASSWORD")
}

MISSING = object()  # dom value meaning "lookup raises NoSuchElementException"


class FakeClock:
    """Stands in for `time` inside selenium's WebDriverWait: every poll sleep
    jumps far past the timeout, so a wait that never succeeds fails after two
    polls, identically on every run."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += 1000


class FakeTime:
    """Stands in for `time` inside a scraper module: sleeps are recorded."""

    def __init__(self, trace):
        self._trace = trace

    def sleep(self, seconds):
        self._trace.append(["sleep", seconds])

    def time(self):
        return 0.0


class FakeElement:
    def __init__(self, trace, label, spec=None):
        self._trace = trace
        self.label = label
        spec = spec or {}
        self.text = spec.get("text", "")
        self._attrs = spec.get("attrs", {})
        self._displayed = spec.get("displayed", True)
        self._enabled = spec.get("enabled", True)
        self._dom = spec.get("dom", {})

    def __repr__(self):
        return self.label

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name, "")

    def click(self):
        self._trace.append(["click", self.label])

    def clear(self):
        self._trace.append(["clear", self.label])

    def send_keys(self, *keys):
        self._trace.append(["send_keys", self.label, list(keys)])

    def find_element(self, by, value):
        self._trace.append(["find_element", self.label, by, value])
        return _lookup_one(self._trace, self._dom, by, value, f"{self.label}>{by}={value}")

    def find_elements(self, by, value):
        self._trace.append(["find_elements", self.label, by, value])
        return _lookup_many(self._trace, self._dom, by, value, f"{self.label}>{by}={value}")


def _lookup_one(trace, dom, by, value, label):
    entry = dom.get((by, value))
    if entry is MISSING:
        raise NoSuchElementException(f"no element for {by}={value}")
    return FakeElement(trace, label, entry[0] if entry else None)


def _lookup_many(trace, dom, by, value, label):
    entry = dom.get((by, value))
    if entry is None or entry is MISSING:
        return []
    return [FakeElement(trace, f"{label}#{i}", spec) for i, spec in enumerate(entry)]


class FakeChrome:
    """Records the driver-level calls. Class attributes are set per scenario."""

    trace = None
    dom = {}
    scripts = {}

    def __init__(self, options=None):
        self.trace.append(["chrome", {
            "arguments": list(options.arguments) if options else [],
            "prefs": options.experimental_options if options else {},
        }])

    def get(self, url):
        self.trace.append(["get", url])

    def refresh(self):
        self.trace.append(["refresh"])

    def quit(self):
        self.trace.append(["quit"])

    def find_element(self, by, value):
        self.trace.append(["find_element", by, value])
        return _lookup_one(self.trace, self.dom, by, value, f"{by}={value}")

    def find_elements(self, by, value):
        self.trace.append(["find_elements", by, value])
        return _lookup_many(self.trace, self.dom, by, value, f"{by}={value}")

    def execute_script(self, script, *args):
        labels = [str(a) for a in args]
        self.trace.append(["execute_script", script, labels])
        return self.scripts.get(labels[0]) if labels else None


class _TraceLogHandler(logging.Handler):
    def __init__(self, trace):
        super().__init__(logging.INFO)
        self._trace = trace

    def emit(self, record):
        self._trace.append(["log", record.levelname, record.getMessage()])


def spec(text="", **kw):
    return {"text": text, **kw}


# fake ids, shaped like WebJoint's "<company>-<facility>-<customer>-<order>"
ORDER_ID = "1111-2222-3333333-4444444"
OTHER_ORDER_ID = "1111-2222-5555555-6666666"
ORDER_TITLE = (By.CSS_SELECTOR, '[data-test="page-title"]')


def order_page(order_id=ORDER_ID):
    """DOM entry for the order page's heading, as shown for `order_id`."""
    return {ORDER_TITLE: [spec(f"Order # {order_id}")]}


@override_settings(HEADLESS=False)
class ScraperTraceTests(SimpleTestCase):
    def run_scenario(self, name, fn, dom=None, scripts=None, extra_patches=()):
        trace = []
        chrome = type("ScenarioChrome", (FakeChrome,), {
            "trace": trace, "dom": dom or {}, "scripts": scripts or {},
        })
        handler = _TraceLogHandler(trace)
        orders_logger = logging.getLogger("orders")
        with ExitStack() as stack:
            stack.enter_context(patch("selenium.webdriver.Chrome", chrome))
            stack.enter_context(patch("selenium.webdriver.support.wait.time", FakeClock()))
            for module in (webjoint, talkroute, weedmaps, merchant_pay):
                stack.enter_context(patch.object(module, "time", FakeTime(trace)))
            stack.enter_context(patch.dict(os.environ, FAKE_ENV))
            for p in extra_patches:
                stack.enter_context(p)
            orders_logger.addHandler(handler)
            try:
                result = fn()
            finally:
                orders_logger.removeHandler(handler)
        trace.append(["return", result])
        self.assert_trace(name, trace)

    def assert_trace(self, name, trace):
        actual = json.dumps(trace, indent=1, ensure_ascii=False, default=str) + "\n"
        path = TRACE_DIR / f"{name}.json"
        if os.environ.get("UPDATE_TRACES") == "1":
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
            return
        self.assertTrue(path.exists(), f"missing golden trace {path}; generate with UPDATE_TRACES=1")
        expected = path.read_text(encoding="utf-8")
        if actual != expected:
            diff = "".join(difflib.unified_diff(
                expected.splitlines(True), actual.splitlines(True),
                fromfile=f"golden/{name}.json", tofile="actual", n=3,
            ))
            self.fail(f"scraper trace for {name} changed:\n{diff}")

    # ── webjoint ──────────────────────────────────────────────────────────

    def test_fill_order_notes(self):
        notes_xpath = '/html/body/div[4]/div[2]/div[2]/form/div[1]/div/div/div/div/div/textarea[3]'
        self.run_scenario(
            "webjoint_fill_order_notes",
            lambda: webjoint.fill_order_notes(ORDER_ID, "test note"),
            dom={**order_page(), (By.XPATH, notes_xpath): [spec(attrs={"value": "old note"})]},
        )

    def test_fill_order_notes_wrong_order(self):
        # the page that opened is another customer's order: nothing may be typed or saved
        self.run_scenario(
            "webjoint_fill_order_notes_wrong_order",
            lambda: webjoint.fill_order_notes(ORDER_ID, "test note"),
            dom=order_page(OTHER_ORDER_ID),
        )

    def test_pull_from_chiles_wrong_order(self):
        self.run_scenario(
            "webjoint_pull_from_chiles_wrong_order",
            lambda: webjoint.pull_from_chiles(ORDER_ID),
            dom=order_page(OTHER_ORDER_ID),
        )

    def test_dispatch_to_driver_wrong_order(self):
        self.run_scenario(
            "webjoint_dispatch_to_driver_wrong_order",
            lambda: webjoint.dispatch_to_driver(ORDER_ID),
            dom=order_page(OTHER_ORDER_ID),
        )

    def test_open_order_raises_on_mismatched_page(self):
        trace = []
        chrome = type("ScenarioChrome", (FakeChrome,), {
            "trace": trace, "dom": order_page(OTHER_ORDER_ID), "scripts": {},
        })
        driver = chrome()
        wait = WebDriverWait(driver, 10)
        with patch("selenium.webdriver.support.wait.time", FakeClock()):
            with self.assertRaises(webjoint.WrongOrderError) as caught:
                webjoint._open_order(driver, wait, ORDER_ID)
        self.assertEqual((caught.exception.expected, caught.exception.seen), (ORDER_ID, OTHER_ORDER_ID))

    def test_open_order_raises_when_no_order_heading(self):
        driver = type("ScenarioChrome", (FakeChrome,), {"trace": [], "dom": {}, "scripts": {}})()
        with patch("selenium.webdriver.support.wait.time", FakeClock()):
            with self.assertRaises(webjoint.WrongOrderError) as caught:
                webjoint._open_order(driver, WebDriverWait(driver, 10), ORDER_ID)
        self.assertIsNone(caught.exception.seen)

    def test_open_order_returns_on_matching_page(self):
        trace = []
        driver = type("ScenarioChrome", (FakeChrome,), {"trace": trace, "dom": order_page(), "scripts": {}})()
        with patch("selenium.webdriver.support.wait.time", FakeClock()):
            webjoint._open_order(driver, WebDriverWait(driver, 10), ORDER_ID)
        self.assertIn(["get", f"{webjoint._WEBJOINT_URL}orders/{ORDER_ID}/show"], trace)
        self.assertIn(["refresh"], trace)

    def test_open_order_refuses_blank_id(self):
        driver = type("ScenarioChrome", (FakeChrome,), {"trace": [], "dom": {}, "scripts": {}})()
        with self.assertRaises(webjoint.WrongOrderError):
            webjoint._open_order(driver, WebDriverWait(driver, 10), "")

    def test_pull_from_chiles_available(self):
        rows = '//tbody/tr'
        dialog = '//div[@role="dialog"]'
        qty = lambda i: f"packagequantities[{i}].nfrompackagequantity"
        dom = {
            **order_page(),
            (By.XPATH, rows): [
                spec("Item A\nStarting Shift Mobile Inventory"),
                spec("Item B\nChiles Rd Shop"),
                spec("Item C\nChiles Rd Shop\nClosing Shift Mobile Inventory"),
            ],
            (By.XPATH, '//*[@role="option" and contains(.,"@ Chiles Rd Shop")]'): [spec("@ Chiles Rd Shop")],
            (By.XPATH, dialog): [spec(dom={(By.CLASS_NAME, "c420"): [spec(), spec()]})],
            (By.ID, qty(0)): [spec(attrs={"value": "3"})],
            (By.ID, qty(1)): [spec(attrs={"value": "2"})],
        }
        scripts = {
            f"id={qty(0)}": "Chiles Rd Shop",
            f"id={qty(1)}": "Closing Shift Mobile Inventory",
        }
        self.run_scenario(
            "webjoint_pull_from_chiles_available",
            lambda: webjoint.pull_from_chiles(ORDER_ID), dom=dom, scripts=scripts,
        )

    def test_pull_from_chiles_kit_removal(self):
        dom = {
            **order_page(),
            (By.XPATH, '//tbody/tr'): [spec("Item K\nStarting Shift Mobile Inventory")],
            (By.XPATH, '//*[@role="option" and contains(.,"@ Starting Shift Mobile Inventory")]'):
                [spec("@ Starting Shift Mobile Inventory")],
        }
        self.run_scenario(
            "webjoint_pull_from_chiles_kit_removal", lambda: webjoint.pull_from_chiles(ORDER_ID), dom=dom,
        )

    def test_dispatch_to_driver(self):
        self.run_scenario(
            "webjoint_dispatch_to_driver",
            lambda: webjoint.dispatch_to_driver(ORDER_ID),
            dom=order_page(),
        )

    @staticmethod
    def _product(**overrides):
        product = {
            "product_name": "Beach Party | Refined Live Resin | All-In-One",
            "brand": "Raw Garden",
            "product_type": "Concentrate",
            "lineage": "Hybrid",
            "strain": "Beach Party",
            "category": "Cartridges",
            "subcategory": "Live Resin & Live Rosin | 510 & All-In-One Disposable",
            "seo_title": "Beach Party Refined Live Resin AIO Raw Garden",
            "description": "Bright and fruity.",
            "sale_discount": "10%",
            "tags": "",
            "for_sale": "Yes",
            "weight_size": 1,
            "unit": "g",
            "name_variant": "Beach Party AIO",
            "price": 49.99,
            "cannabinoids": {},
        }
        product.update(overrides)
        return product

    @staticmethod
    def _product_dom(options):
        submit = (
            f'//button[@type="submit"][{ci_contains("create product")} or {ci_contains("save changes")}]'
        )
        return {
            (By.XPATH, '//*[@role="option"]'): [spec(t) for t in options],
            (By.XPATH, submit): [spec("Create Product")],
        }

    def test_create_product_full(self):
        # options cover all three autocomplete branches: exact match (brand),
        # 'Create "..."' (strain), and neither -> ENTER (everything else)
        dom = self._product_dom(["Hybrid", "Raw Garden", 'Create "Beach Party"'])
        product = self._product(cannabinoids={"THC": "34.5%", "Total CBD": "1mg"})
        self.run_scenario(
            "webjoint_create_product_full",
            lambda: webjoint.create_product([product]), dom=dom,
        )

    def test_create_product_minimal(self):
        dom = self._product_dom([])
        product = self._product(lineage="", strain="", for_sale="No", sale_discount="0%")
        self.run_scenario(
            "webjoint_create_product_minimal",
            lambda: webjoint.create_product([product]), dom=dom,
        )

    def test_set_webhooks(self):
        self.run_scenario(
            "webjoint_set_webhooks",
            lambda: webjoint.set_webhooks("https://example.ngrok.app"),
        )

    def test_get_customer_info(self):
        count_xpath = "/html/body/div[1]/div/div/main/div[2]/div/div/form/div[2]/span[3]/div/div/div/span/div/span[2]"
        self.run_scenario(
            "webjoint_get_customer_info",
            webjoint.get_customer_info,
            dom={(By.XPATH, count_xpath): [spec("Order count 7")]},
        )

    # ── other sites ───────────────────────────────────────────────────────

    def test_talkroute_send_message(self):
        self.run_scenario(
            "talkroute_send_message",
            lambda: talkroute.send_message("5555550100", "hello"),
        )

    def test_weedmaps_get_wm_payment_type(self):
        field = '//*[@id="__next"]/div/div[2]/div/div[3]/div[3]/div[1]/div[1]/div/div/div[4]/div[1]/p[1]'
        header = '//span[normalize-space(.)="Order #49018286"]'
        self.run_scenario(
            "weedmaps_get_wm_payment_type",
            lambda: weedmaps.get_wm_payment_type(49018286),
            dom={(By.XPATH, header): [spec("Order #49018286")], (By.XPATH, field): [spec("Cashless")]},
        )

    def test_weedmaps_get_wm_payment_type_order_not_found(self):
        # the order's link/page never appears: Cash, and the payment field is never read
        field = '//*[@id="__next"]/div/div[2]/div/div[3]/div[3]/div[1]/div[1]/div/div/div[4]/div[1]/p[1]'
        self.run_scenario(
            "weedmaps_get_wm_payment_type_not_found",
            lambda: weedmaps.get_wm_payment_type(49018286),
            dom={(By.XPATH, field): [spec("Cashless")], **self._missing_wm_order()},
        )

    @staticmethod
    def _missing_wm_order():
        link = '//a[starts-with(@href, "/orders/") and normalize-space(.)="#49018286"]'
        return {(By.XPATH, link): MISSING}

    def test_weedmaps_get_wm_payment_type_without_id(self):
        # no Weedmaps number in the webhook: no browser at all
        self.run_scenario(
            "weedmaps_get_wm_payment_type_no_id",
            lambda: weedmaps.get_wm_payment_type(None),
        )

    def test_merchant_pay_send_merchant_pay_link(self):
        self.run_scenario(
            "merchant_pay_send_merchant_pay_link",
            merchant_pay.send_merchant_pay_link,
            extra_patches=[patch.object(merchant_pay, "GetCode", lambda: "123456")],
        )
