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
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from orders.services.scrapers import browser, merchant_pay, talkroute, webjoint, weedmaps
from orders.services.scrapers.browser import ci_contains
from orders.services.scrapers.errors import BeforeCommitError

TRACE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "scraper_traces"

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
        self._click_error = spec.get("click_error")

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
        if self._click_error:
            raise RuntimeError(self._click_error)

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
        self.trace.append(
            [
                "chrome",
                {
                    "arguments": list(options.arguments) if options else [],
                    "prefs": options.experimental_options if options else {},
                },
            ]
        )

    def get(self, url):
        self.trace.append(["get", url])

    def set_page_load_timeout(self, seconds):
        self.trace.append(["set_page_load_timeout", seconds])

    def refresh(self):
        self.trace.append(["refresh"])

    def quit(self):
        self.trace.append(["quit"])

    # failure artifacts: only the file name is recorded (the folder differs per machine)
    def save_screenshot(self, path):
        self.trace.append(["save_screenshot", Path(path).name])
        return True

    @property
    def page_source(self):
        self.trace.append(["page_source"])
        return "<html></html>"

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


FIXED_NOW = datetime(2026, 7, 20, 14, 0, 0)


@override_settings(HEADLESS=False)
class ScraperTraceTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        override = override_settings(FAILURE_DIR=folder.name)
        override.enable()
        self.addCleanup(override.disable)
        self.failure_dir = Path(folder.name)

    def run_scenario(self, name, fn, dom=None, scripts=None, extra_patches=()):
        trace = []
        chrome = type(
            "ScenarioChrome",
            (FakeChrome,),
            {
                "trace": trace,
                "dom": dom or {},
                "scripts": scripts or {},
            },
        )
        handler = _TraceLogHandler(trace)
        orders_logger = logging.getLogger("orders")
        with ExitStack() as stack:
            stack.enter_context(patch("selenium.webdriver.Chrome", chrome))
            stack.enter_context(patch("selenium.webdriver.support.wait.time", FakeClock()))
            for module in (webjoint, talkroute, weedmaps, merchant_pay):
                stack.enter_context(patch.object(module, "time", FakeTime(trace)))
            stack.enter_context(override_settings(**FAKE_ENV))
            stack.enter_context(patch.object(browser, "_now", lambda: FIXED_NOW))
            for p in extra_patches:
                stack.enter_context(p)
            orders_logger.addHandler(handler)
            try:
                result = fn()
            except Exception as exc:
                # a scraper that fails raises; the trace records which kind and at which step
                trace.append(["raised", type(exc).__name__, getattr(exc, "step", None), str(exc)])
                result = None
                self.raised = exc
            else:
                self.raised = None
            finally:
                orders_logger.removeHandler(handler)
        trace.append(["return", result])
        self.trace, self.result = trace, result
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
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(True),
                    actual.splitlines(True),
                    fromfile=f"golden/{name}.json",
                    tofile="actual",
                    n=3,
                )
            )
            self.fail(f"scraper trace for {name} changed:\n{diff}")

    # ── webjoint ──────────────────────────────────────────────────────────

    def test_fill_order_notes(self):
        notes_xpath = "/html/body/div[4]/div[2]/div[2]/form/div[1]/div/div/div/div/div/textarea[3]"
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
        chrome = type(
            "ScenarioChrome",
            (FakeChrome,),
            {
                "trace": trace,
                "dom": order_page(OTHER_ORDER_ID),
                "scripts": {},
            },
        )
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
        rows = webjoint._ITEM_ROWS_XPATH
        dialog = '//div[@role="dialog"]'

        def qty(i):
            return f"packagequantities[{i}].nfrompackagequantity"

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
            lambda: webjoint.pull_from_chiles(ORDER_ID),
            dom=dom,
            scripts=scripts,
        )

    def test_pull_from_chiles_kit_removal(self):
        dom = {
            **order_page(),
            (By.XPATH, webjoint._ITEM_ROWS_XPATH): [spec("Item K\nStarting Shift Mobile Inventory")],
            (By.XPATH, '//*[@role="option" and contains(.,"@ Starting Shift Mobile Inventory")]'): [
                spec("@ Starting Shift Mobile Inventory")
            ],
        }
        self.run_scenario(
            "webjoint_pull_from_chiles_kit_removal",
            lambda: webjoint.pull_from_chiles(ORDER_ID),
            dom=dom,
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
        submit = f'//button[@type="submit"][{ci_contains("create product")} or {ci_contains("save changes")}]'
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
            lambda: webjoint.create_product([product]),
            dom=dom,
        )

    def test_create_product_minimal(self):
        dom = self._product_dom([])
        product = self._product(lineage="", strain="", for_sale="No", sale_discount="0%")
        self.run_scenario(
            "webjoint_create_product_minimal",
            lambda: webjoint.create_product([product]),
            dom=dom,
        )

    def test_set_webhooks(self):
        self.run_scenario(
            "webjoint_set_webhooks",
            lambda: webjoint.set_webhooks("https://example.ngrok.app"),
        )

    # ── failures: before the commit point (retryable) vs at/after it ──────
    #
    # BeforeCommitError means nothing irreversible happened, so the task layer
    # retries. Any other error comes from the commit click or later and must
    # never be retried automatically.

    def assert_raised(self, kind, step):
        self.assertIsNotNone(self.raised, "the scraper was expected to raise")
        self.assertEqual((type(self.raised).__name__, getattr(self.raised, "step", None)), (kind, step))
        self.assertEqual(isinstance(self.raised, BeforeCommitError), kind in ("BeforeCommitError", "WrongOrderError"))

    def test_talkroute_fails_before_send(self):
        self.run_scenario(
            "talkroute_send_message_fails_before_send",
            lambda: talkroute.send_message("5555550100", "hello", order_ref="9001"),
            dom={(By.ID, "conversations-to-phone-number-input"): MISSING},
        )
        self.assert_raised("BeforeCommitError", "compose message")

    def test_talkroute_fails_at_the_send_click(self):
        self.run_scenario(
            "talkroute_send_message_fails_at_send",
            lambda: talkroute.send_message("5555550100", "hello", order_ref="9001"),
            dom={(By.CLASS_NAME, "message-input-send"): [spec(click_error="send click failed")]},
        )
        self.assert_raised("RuntimeError", "send message")

    def test_fill_order_notes_fails_before_save(self):
        notes_xpath = "/html/body/div[4]/div[2]/div[2]/form/div[1]/div/div/div/div/div/textarea[3]"
        self.run_scenario(
            "webjoint_fill_order_notes_fails_before_save",
            lambda: webjoint.fill_order_notes(ORDER_ID, "test note"),
            dom={**order_page(), (By.XPATH, notes_xpath): MISSING},
        )
        self.assert_raised("BeforeCommitError", "edit notes")

    def test_fill_order_notes_fails_at_the_save_click(self):
        save = "/html/body/div[4]/div[2]/div[2]/form/div[2]/button"
        self.run_scenario(
            "webjoint_fill_order_notes_fails_at_save",
            lambda: webjoint.fill_order_notes(ORDER_ID, "test note"),
            dom={**order_page(), (By.XPATH, save): [spec(click_error="save click failed")]},
        )
        self.assert_raised("RuntimeError", "save notes")

    def test_dispatch_fails_before_confirm(self):
        confirm = "/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]"
        self.run_scenario(
            "webjoint_dispatch_to_driver_fails_before_confirm",
            lambda: webjoint.dispatch_to_driver(ORDER_ID),
            dom={**order_page(), (By.XPATH, confirm): MISSING},
        )
        self.assert_raised("BeforeCommitError", "assign driver")

    def test_item_rows_are_only_the_rows_with_an_edit_button(self):
        # `//tbody/tr` also matched the TAX, DELIVERY and driver-assignment rows, so after the items
        # were pulled the scraper tried to "edit" the driver row and reported a false failure
        self.assertIn(webjoint._EDIT_BUTTON_XPATH, webjoint._ITEM_ROWS_XPATH)
        self.assertNotEqual(webjoint._ITEM_ROWS_XPATH, "//tbody/tr")
        self.assertNotIn('"//tbody/tr"', Path(webjoint.__file__).read_text())

    def _prepare_dom(self, extra=None):
        return {
            **order_page(),
            (By.XPATH, webjoint._ITEM_ROWS_XPATH): [spec("Item B\nChiles Rd Shop")],
            **(extra or {}),
        }

    def test_prepare_order_dispatches_then_pulls_in_one_login(self):
        self.run_scenario(
            "webjoint_prepare_order", lambda: webjoint.prepare_order(ORDER_ID, True), dom=self._prepare_dom()
        )
        self.assertEqual(self.result, [])
        self.assertEqual(self.trace.count(["get", webjoint._WEBJOINT_URL]), 1)  # ONE login
        flat = json.dumps(self.trace, default=str)
        confirm = "/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]"
        self.assertLess(
            flat.index(confirm), flat.index(webjoint._ITEM_ROWS_XPATH.replace('"', '\\"'))
        )  # dispatch first

    def test_prepare_order_only_pulls_when_not_dispatching(self):
        self.run_scenario(
            "webjoint_prepare_order_pull_only", lambda: webjoint.prepare_order(ORDER_ID, False), dom=self._prepare_dom()
        )
        self.assertEqual(self.result, [])
        self.assertNotIn("selectActionButton", json.dumps(self.trace, default=str))

    def test_prepare_order_a_failed_dispatch_does_not_stop_the_pull(self):
        confirm = "/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]"
        self.run_scenario(
            "webjoint_prepare_order_dispatch_fails_pull_still_runs",
            lambda: webjoint.prepare_order(ORDER_ID, True),
            dom=self._prepare_dom({(By.XPATH, confirm): MISSING}),
        )
        [failure] = self.result
        self.assertEqual(
            (failure.step, type(failure.error).__name__, failure.error.step),
            ("dispatch_to_driver", "BeforeCommitError", "assign driver"),
        )
        self.assertIn(
            webjoint._ITEM_ROWS_XPATH.replace('"', '\\"'), json.dumps(self.trace, default=str)
        )  # the pull still ran
        # the failed step keeps its failure capture, named for it, as it had in a session of its
        # own (the fake browser writes no screenshot, but the page source is written for real)
        self.assertEqual(
            sorted(path.name for path in self.failure_dir.iterdir()),
            [f"dispatch_to_driver-{ORDER_ID}-20260720-140000.html"],
        )

    def test_prepare_order_a_failed_login_raises_before_any_step(self):
        self.run_scenario(
            "webjoint_prepare_order_login_fails",
            lambda: webjoint.prepare_order(ORDER_ID, True),
            dom=self._prepare_dom({(By.CLASS_NAME, "c199.c13"): MISSING}),
        )
        self.assert_raised("BeforeCommitError", "login")
        self.assertNotIn(f"orders/{ORDER_ID}/show", json.dumps(self.trace, default=str))  # no order was opened

    def test_dispatch_fails_at_the_confirm_click(self):
        confirm = "/html/body/div[5]/div[2]/div/div[2]/div[2]/button[1]/span[1]"
        self.run_scenario(
            "webjoint_dispatch_to_driver_fails_at_confirm",
            lambda: webjoint.dispatch_to_driver(ORDER_ID),
            dom={**order_page(), (By.XPATH, confirm): [spec(click_error="confirm click failed")]},
        )
        self.assert_raised("RuntimeError", "confirm dispatch")

    def test_pull_from_chiles_row_fails_before_any_save(self):
        dom = {
            **order_page(),
            (By.XPATH, webjoint._ITEM_ROWS_XPATH): [spec("Item A\nStarting Shift Mobile Inventory")],
            (By.CSS_SELECTOR, "div.css-1j4kz5m"): MISSING,
        }
        self.run_scenario(
            "webjoint_pull_from_chiles_fails_before_save",
            lambda: webjoint.pull_from_chiles(ORDER_ID),
            dom=dom,
        )
        self.assert_raised("BeforeCommitError", "row 0")

    def test_pull_from_chiles_row_fails_after_an_earlier_save(self):
        edit = './/button[.//*[local-name()="path"][contains(@d,"M3 17.25")]]'
        dom = {
            **order_page(),
            (By.XPATH, webjoint._ITEM_ROWS_XPATH): [
                spec("Item A\nStarting Shift Mobile Inventory"),
                spec("Item B\nStarting Shift Mobile Inventory", dom={(By.XPATH, edit): MISSING}),
            ],
            (By.XPATH, '//*[@role="option" and contains(.,"@ Chiles Rd Shop")]'): [spec("@ Chiles Rd Shop")],
        }
        self.run_scenario(
            "webjoint_pull_from_chiles_fails_after_save",
            lambda: webjoint.pull_from_chiles(ORDER_ID),
            dom=dom,
        )
        # row 0 was saved, so the failure of row 1 must not be retried automatically
        self.assert_raised("NoSuchElementException", "row 1")

    def test_create_product_fails_before_any_submit(self):
        submit = f'//button[@type="submit"][{ci_contains("create product")} or {ci_contains("save changes")}]'
        dom = {**self._product_dom([]), (By.XPATH, submit): MISSING}
        self.run_scenario(
            "webjoint_create_product_fails_before_submit",
            lambda: webjoint.create_product([self._product()]),
            dom=dom,
        )
        self.assert_raised("BeforeCommitError", "create product 'Beach Party | Refined Live Resin | All-In-One'")

    def test_create_product_second_product_fails_after_the_first_was_submitted(self):
        dom = self._product_dom(["Hybrid"])  # no "Sativa" option for the second product
        first = self._product(lineage="", strain="", product_name="First")
        second = self._product(lineage="Sativa", product_name="Second")
        self.run_scenario(
            "webjoint_create_product_fails_after_submit",
            lambda: webjoint.create_product([first, second]),
            dom=dom,
        )
        self.assert_raised("ValueError", "create product 'Second'")

    def test_set_webhooks_fails_at_the_save_click_and_still_waits(self):
        # click_with_js_fallback swallows a failed native click and retries with JS, so the
        # failure is injected into the helper itself
        self.run_scenario(
            "webjoint_set_webhooks_fails_at_save",
            lambda: webjoint.set_webhooks("https://example.ngrok.app"),
            extra_patches=[
                patch.object(webjoint, "click_with_js_fallback", side_effect=RuntimeError("save click failed"))
            ],
        )
        self.assert_raised("RuntimeError", "save webhooks")

    def test_failure_screenshot_and_page_source_are_written(self):
        self.run_scenario(
            "webjoint_fill_order_notes_wrong_order",
            lambda: webjoint.fill_order_notes(ORDER_ID, "test note"),
            dom=order_page(OTHER_ORDER_ID),
        )
        # the fake browser writes no screenshot, but the page source is written for real
        names = sorted(path.name for path in self.failure_dir.iterdir())
        self.assertEqual(names, [f"fill_order_notes-{ORDER_ID}-20260720-140000.html"])

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

    def _wm_found_order_dom(self):
        field = '//*[@id="__next"]/div/div[2]/div/div[3]/div[3]/div[1]/div[1]/div/div/div[4]/div[1]/p[1]'
        header = '//span[normalize-space(.)="Order #49018286"]'
        return {(By.XPATH, header): [spec("Order #49018286")], (By.XPATH, field): [spec("Cashless")]}

    def test_weedmaps_login_page_that_never_finishes_loading_is_not_fatal(self):
        # the page load times out but the form is usable: carry on and log in
        def slow_get(chrome, url):
            chrome.trace.append(["get", url])
            raise TimeoutException("page load timed out")

        self.run_scenario(
            "weedmaps_get_wm_payment_type_slow_login_page",
            lambda: weedmaps.get_wm_payment_type(49018286),
            dom=self._wm_found_order_dom(),
            extra_patches=[patch.object(FakeChrome, "get", slow_get)],
        )
        self.assertIsNone(self.raised)

    def test_weedmaps_login_page_that_cannot_be_reached_is_retry_safe(self):
        # nothing has happened yet, so this is a BeforeCommitError the task retries by itself
        def unreachable_get(chrome, url):
            chrome.trace.append(["get", url])
            raise WebDriverException("net::ERR_INTERNET_DISCONNECTED")

        self.run_scenario(
            "weedmaps_get_wm_payment_type_login_page_unreachable",
            lambda: weedmaps.get_wm_payment_type(49018286),
            extra_patches=[patch.object(FakeChrome, "get", unreachable_get)],
        )
        self.assert_raised("BeforeCommitError", "open Weedmaps login page")

    def test_weedmaps_tells_chrome_not_to_wait_for_every_resource(self):
        session = MagicMock(wraps=weedmaps.browser_session)
        self.run_scenario(
            "weedmaps_get_wm_payment_type",
            lambda: weedmaps.get_wm_payment_type(49018286),
            dom=self._wm_found_order_dom(),
            extra_patches=[patch.object(weedmaps, "browser_session", session)],
        )
        self.assertEqual(session.call_args.kwargs["page_load_strategy"], "eager")

    def test_create_driver_uses_a_saved_profile_only_when_given_one(self):
        with patch("selenium.webdriver.Chrome") as chrome:
            browser.create_driver(user_data_dir="/profiles/talkroute")
        self.assertIn("--user-data-dir=/profiles/talkroute", chrome.call_args.kwargs["options"].arguments)
        with patch("selenium.webdriver.Chrome") as chrome:
            browser.create_driver()
        self.assertFalse([a for a in chrome.call_args.kwargs["options"].arguments if a.startswith("--user-data-dir")])

    def test_create_driver_headless_can_be_overridden_either_way(self):
        with override_settings(HEADLESS=True), patch("selenium.webdriver.Chrome") as chrome:
            browser.create_driver()
            self.assertIn("--headless=new", chrome.call_args.kwargs["options"].arguments)
            browser.create_driver(headless=False)
            self.assertNotIn("--headless=new", chrome.call_args.kwargs["options"].arguments)
        with override_settings(HEADLESS=False), patch("selenium.webdriver.Chrome") as chrome:
            browser.create_driver(headless=True)
            self.assertIn("--headless=new", chrome.call_args.kwargs["options"].arguments)

    def test_talkroute_uses_its_saved_profile(self):
        session = MagicMock(wraps=talkroute.browser_session)
        self.run_scenario(
            "talkroute_send_message",
            lambda: talkroute.send_message("5555550100", "hello"),
            extra_patches=[patch.object(talkroute, "browser_session", session)],
        )
        self.assertEqual(session.call_args.kwargs["user_data_dir"], "/tmp/tgpf-test-profiles/talkroute")

    def test_create_driver_waits_for_the_full_load_unless_told_otherwise(self):
        with patch("selenium.webdriver.Chrome") as chrome:
            browser.create_driver(page_load_strategy="eager")
        self.assertEqual(chrome.call_args.kwargs["options"].page_load_strategy, "eager")
        with patch("selenium.webdriver.Chrome") as chrome:
            browser.create_driver()
        self.assertEqual(chrome.call_args.kwargs["options"].page_load_strategy, "normal")

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
            extra_patches=[patch.object(merchant_pay, "fetch_2fa_code", lambda: "123456")],
        )
