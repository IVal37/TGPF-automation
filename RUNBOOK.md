One command, starts everything below (Ctrl+C stops everything):

    ./run.sh

Redis/Celery output is backgrounded to logs/ since run.sh only shows Django directly — tail those if something looks off, e.g. `tail -f logs/celery_browser.log` (browser jobs) or `logs/celery_worker.log`.

Manual steps below — same things (the Celery worker is two processes), useful if you need to restart just one piece without killing the rest.

Before all of them, once per shell:

    cd /home/izaak/TGPFflows && source .venv/bin/activate

1. MySQL
    sudo service mysql start

2. Redis
    redis-server

3. Celery workers (two terminals). The first runs the default queue; the second runs every
   browser job (WebJoint, TalkRoute, Weedmaps, merchant pay) one at a time. If the browser
   worker is not running, those jobs wait silently in Redis and nothing is texted or noted.
    celery -A TGPFflows worker -Q celery --loglevel=info
    celery -A TGPFflows worker -Q browser -c 1 -n browser@%h --loglevel=info

4. Celery beat
    celery -A TGPFflows beat --loglevel=info

5. Django
    python manage.py runserver

First-time setup / security (once):

    # in .env (see .env.example): DJANGO_SECRET_KEY and WEBHOOK_SECRET are required,
    # the server will not start without them. Generate values with:
    python -c "import secrets; print(secrets.token_urlsafe(50))"   # DJANGO_SECRET_KEY
    python -c "import secrets; print(secrets.token_urlsafe(32))"   # WEBHOOK_SECRET
    python manage.py createsuperuser    # the dashboard login at /accounts/login/ (MySQL must be running)

WebJoint's webhooks are re-registered with the secret URLs (hooks/<WEBHOOK_SECRET>/new_order ...) every time the server starts. Changing WEBHOOK_SECRET needs a restart.

Running tests (in-memory SQLite, MySQL does not need to be running):

    python manage.py test orders --settings=orders.test_settings

Lint and format (`pip install -r requirements-dev.txt` once):

    ruff check . && ruff format --check .

Scraper tests compare recorded browser action traces to golden files in orders/fixtures/scraper_traces/. After an intentional scraper change, rewrite them with:

    UPDATE_TRACES=1 python manage.py test orders.tests.test_scrapers --settings=orders.test_settings

Checking a channel works (both really send, and refuse to run while `RESTOCK_TEST_MODE=true`):

    python manage.py send_test_message --sms        # texts TALKROUTE_TEST_PHONE
    python manage.py send_test_message --telegram   # posts "testing" to the restock channel

Checking dependencies for known vulnerabilities (needs requirements-dev.txt):

    pip-audit -r requirements.txt --no-deps --disable-pip
