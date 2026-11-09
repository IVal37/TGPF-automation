# TGPFflows

Django + MySQL dispatch automation. WebJoint webhooks come in; Selenium scrapers (WebJoint, TalkRoute, Weedmaps, merchant pay) and Celery tasks do the work. See RUNBOOK.md for starting the stack.

## Tests

    .venv/bin/python manage.py test orders --settings=orders.test_settings

Runs on in-memory SQLite, so MySQL does not need to be running. `orders/tests_scrapers.py` runs every scraper function against a fake Chrome and compares the recorded trace (lookups, clicks, keys, scripts, sleeps, logged messages, return value) to golden JSON in `orders/fixtures/scraper_traces/`.

## Rules

- **Line endings:** `orders/services/engine.py`, `orders/services/scrapers/talkroute.py` and `orders/services/scrapers/merchant_pay.py` use CRLF and must keep it. Everything else is LF. Scripted rewrites (`read_text`/`write_text`) silently convert to LF, so check with `file <path>` after editing.
- **Sleeps:** never move, drop or change a `time.sleep` in a scraper unless asked. The values are tuned against the live sites.
- **Scraper changes:** the golden traces must be updated deliberately with `UPDATE_TRACES=1 .venv/bin/python manage.py test orders.tests_scrapers --settings=orders.test_settings`. Then review the diff in `orders/fixtures/scraper_traces/` and explain each trace change. A trace diff you can't explain is a bug.
- **`RESTOCK_TEST_MODE`:** must gate every SMS and WebJoint side effect (anything that texts a customer or writes to WebJoint), in views and anywhere else those are triggered.
- **Secrets:** `DJANGO_SECRET_KEY` and `WEBHOOK_SECRET` must be set or settings refuses to import (tests get dummy values from `orders/test_settings.py`). The webhook secret is part of the URL path; never log or print a full webhook URL without `redact_webhook_key()`. `DEBUG` is off unless `DJANGO_DEBUG=true`.
- **Git:** never commit or push unless asked.

## Adding things

- **WebJoint automation:** write the function in `orders/services/scrapers/webjoint.py` with `@_webjoint_session` (it logs in and injects `driver, wait`). Add a `@shared_task` wrapper in `orders/tasks.py`, and call it with `.delay()` from the view, gated by `RESTOCK_TEST_MODE`. Add a trace scenario to `tests_scrapers.py`.
- **Another website:** add a module under `orders/services/scrapers/` that uses `create_driver`, `click`, `fill` etc. from `scrapers/browser.py`. Wrap it in a task and add a trace scenario.
- **Scheduled job:** add a task in `orders/tasks.py` and an entry in `CELERY_BEAT_SCHEDULE` in `TGPFflows/settings.py`. Query the DB: the Celery worker is a separate process and cannot see the in-memory state in `orders/services/state.py`.
- **Webhook:** add the route in `orders/urls.py` under `hooks/<str:key>/<name>` and a thin view in `orders/views.py` (parse the request, call a service, return the response), decorated `@csrf_exempt` then `@require_webhook_key` (from `orders/webhooks.py`). Add the name to `HOOK_NAMES` and register it through `webhook_urls()` in `set_webhooks`. Put the logic in `orders/services/`. Never register a bare, keyless webhook path.
- **Dashboard view:** decorate with `@login_required` (outermost), never `@csrf_exempt`, and put `{% csrf_token %}` in every form. Background `fetch()` calls need no extra work; the auto-refresh reloads the page if the session expired.
