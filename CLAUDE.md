# TGPFflows

Django + MySQL dispatch automation. WebJoint webhooks come in; Selenium scrapers (WebJoint, TalkRoute, Weedmaps, merchant pay) and Celery tasks do the work. See RUNBOOK.md for starting the stack.

## Tests

    .venv/bin/python manage.py test orders --settings=orders.test_settings

Runs on in-memory SQLite, so MySQL does not need to be running. The suite is sandboxed and must stay so: `orders/test_settings.py` puts Celery on an in-memory broker and gives dummy logins/keys (Celery prefers the `CELERY_BROKER_URL` *environment variable* over the Django setting, which `.env` sets to the real Redis, so the settings file sets the env var itself), and `orders/tests/__init__.py` refuses to run on a real broker and raises `RealBrowserBlocked` if anything starts a real Chrome. A test that falls through to a real scraper once logged into the live WebJoint and its jobs reached the real worker. When you change what a task calls, update the tests that mock it *before* running the suite. Tests live in the `orders/tests/` package, one module per area (shared fixtures in `tests/helpers.py`). `orders/tests/test_scrapers.py` runs every scraper function against a fake Chrome and compares the recorded trace (lookups, clicks, keys, scripts, sleeps, logged messages, return value) to golden JSON in `orders/fixtures/scraper_traces/`.

Lint and format (config in `pyproject.toml`; `pip install -r requirements-dev.txt` once):

    .venv/bin/ruff check . && .venv/bin/ruff format --check .

## Rules

- **Line endings:** `orders/services/engine.py`, `orders/services/scrapers/talkroute.py` and `orders/services/scrapers/merchant_pay.py` use CRLF and must keep it. Everything else is LF. Scripted rewrites (`read_text`/`write_text`) silently convert to LF, so check with `file <path>` after editing.
- **Sleeps:** never move, drop or change a `time.sleep` in a scraper unless asked. The values are tuned against the live sites.
- **Scraper changes:** the golden traces must be updated deliberately with `UPDATE_TRACES=1 .venv/bin/python manage.py test orders.tests.test_scrapers --settings=orders.test_settings`. Then review the diff in `orders/fixtures/scraper_traces/` and explain each trace change. A trace diff you can't explain is a bug.
- **Environment variables:** read only in `TGPFflows/settings.py` (add new ones there and to `.env.example`); everything else uses `django.conf.settings`.
- **Where logic goes:** views parse the request, call one service and answer. Webhook intake is `services/intake.py` (payloads are parsed once by `services/webhook_payload.py`, which raises `InvalidPayload` -> HTTP 400); the dispatcher's actions are `services/dispatch_queue.py` (raise `ActionRejected` / `NotFound`, which the views turn into a banner message / 404); what the dashboard shows is `services/dashboard.py` and `services/restock.py`; prices, minimums and fees are `services/pricing.py`; customer texts are `services/customer_texts.py`.
- **Secrets:** `DJANGO_SECRET_KEY` and `WEBHOOK_SECRET` must be set or settings refuses to import (tests get dummy values from `orders/test_settings.py`). The webhook secret is part of the URL path; never log or print a full webhook URL without `redact_webhook_key()`. `DEBUG` is off unless `DJANGO_DEBUG=true`.
- **Git:** never commit or push unless asked.

## Adding things

- **WebJoint automation:** write the function in `orders/services/scrapers/webjoint.py` with `@_webjoint_session` (it logs in and injects `driver, wait`). Anything that acts on an order takes `order_id` (the WebJoint `_id`, stored as `Order.webjoint_id`) as its first parameter, opens it with `_open_order` (which raises `WrongOrderError` if the page shows another order), and includes the order ID in its log messages. Never act on "the newest order". Add a `@shared_task` wrapper in `orders/tasks.py`, and call it with `.delay()` (as `tasks.<name>.delay`, from a service, never from a request handler's own logic). Add a trace scenario to `tests/test_scrapers.py`.
- **Another website:** add a module under `orders/services/scrapers/` that uses `create_driver`, `click`, `fill` etc. from `scrapers/browser.py`. Wrap it in a task and add a trace scenario.
- **Browser task:** any task that drives Chrome must be listed in `BROWSER_TASKS` in `TGPFflows/settings.py` so it runs on the single-job `browser` queue. Never call a scraper inside a request; enqueue a task.
- **Scheduled job:** add a task in `orders/tasks.py` and an entry in `CELERY_BEAT_SCHEDULE` in `TGPFflows/settings.py`. Query the DB (helpers in `orders/services/queries.py`): the database is the only source of truth, and the Celery worker is a separate process with no shared memory. A job that posts something (the Telegram digests) must skip itself when it runs long after its scheduled time, because Celery beat re-fires a missed crontab entry the moment the stack starts: pass the schedule's `hour`/`minute` as `kwargs` and check `is_stale_digest` (see `send_third_st_restock`).
- **Webhook:** add the route in `orders/urls.py` under `hooks/<str:key>/<name>` and a thin view in `orders/views.py` (parse the request, call a service, return the response; bad bodies are a 400 via `InvalidPayload`), decorated `@csrf_exempt` then `@require_webhook_key` (from `orders/webhooks.py`). Add the name to `HOOK_NAMES` and register it through `webhook_urls()` in `set_webhooks`. Put the logic in `orders/services/`. Never register a bare, keyless webhook path.
- **Dashboard view:** decorate with `@login_required` (outermost), never `@csrf_exempt`, and put `{% csrf_token %}` in every form. Forms post to `{% url '<name>' %}` (every route is named). A refused action raises `ActionRejected` in the service; the view shows it in the message banner. Background `fetch()` calls need no extra work; the auto-refresh reloads the page if the session expired.
