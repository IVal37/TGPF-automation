# TGPFflows

Dispatch automation for a delivery business. WebJoint sends a webhook when an order is placed; this app
saves it, picks a driver, works out an ETA window, and puts it on a dashboard where a dispatcher approves
and sends the customer's text. Selenium scrapers (WebJoint, TalkRoute, Weedmaps, merchant pay) and Celery
tasks do the browser work; a nightly job posts restock lists to Telegram.

Django 4.2 + MySQL + Redis/Celery. Python 3.8 (the venv); an upgrade is planned.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # add requirements-dev.txt for ruff and pip-audit
cp .env.example .env                       # then fill it in (DJANGO_SECRET_KEY and WEBHOOK_SECRET are required)
python manage.py migrate                   # MySQL must be running
python manage.py createsuperuser           # the dashboard login at /accounts/login/ and /admin/
```

Running the stack (MySQL, Redis, two Celery workers, beat, Django) is in [RUNBOOK.md](RUNBOOK.md); `./run.sh`
starts all of it.

Business settings that used to be in code (city minimums, special customers and addresses, travel times) are
edited in Django admin at `/admin/`.

## Tests and checks

```bash
python manage.py test orders --settings=orders.test_settings   # in-memory SQLite, no MySQL needed
ruff check . && ruff format --check .
```

`RESTOCK_TEST_MODE=true` stops every SMS and WebJoint write while still saving and showing orders; use it
whenever you are trying things out.

## How it fits together

```
WebJoint webhook ──► views.py (parse, answer)
                        │
                        ├─ services/webhook_payload.py   read + validate the body (bad body -> HTTP 400)
                        └─ services/intake.py            save the order, route it, queue texts/notes
                              ├─ services/engine.py          drivers and order lifecycle (complete, cancel, shells)
                              ├─ services/eta.py             route + ETA window   (uses services/travel.py)
                              └─ services/config.py          city minimums, flagged customers, special addresses (DB)

Dashboard (login required)
  views.py ─► services/dashboard.py       the Dispatch Queue cards and driver panel
           ─► services/dispatch_queue.py  the dispatcher's actions: send, reschedule, assign, link, retry, dismiss
           ─► services/restock.py         the Restock tab

Browser and background work
  tasks.py                 Celery tasks; every task that drives Chrome runs on the single-job "browser" queue
  services/scrapers/       Selenium: webjoint.py, talkroute.py (SMS), weedmaps.py, merchant_pay.py, browser.py (shared helpers)
  services/telegram.py     nightly restock digests (scheduled in TGPFflows/settings.py)

Pure rules
  services/pricing.py, customer_texts.py, customers.py, note.py, hours.py, time.py, queries.py
```

Two ideas worth knowing before changing the scrapers (details in the comments of `scrapers/errors.py` and `tasks.py`):

- **Commit points.** A scraper step that fails *before* its irreversible click (sending a text, saving a note)
  raises `BeforeCommitError` and the task is retried automatically. Anything at or after that click is never
  retried, because it may already have gone through, so a customer is never texted twice.
- **The database is the only shared state.** The web server and the Celery workers are separate processes, so
  anything they both need is read from the database (`services/queries.py`).

[CLAUDE.md](CLAUDE.md) has the rules for working in this repo (line endings, scraper sleeps, golden traces,
test mode, secrets) and short recipes for adding a scraper, task, webhook or dashboard view.
