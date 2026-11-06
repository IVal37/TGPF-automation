One command, does all 5 below (Ctrl+C stops everything):

    ./run.sh

Redis/Celery output is backgrounded to logs/ since run.sh only shows Django directly — tail those if something looks off, e.g. `tail -f logs/celery_worker.log`.

Manual steps below — same 5 things, useful if you need to restart just one piece without killing the rest.

Before all 5, once per shell:

    cd /home/izaak/TGPFflows && source .venv/bin/activate

1. MySQL
    sudo service mysql start

2. Redis
    redis-server

3. Celery worker
    celery -A TGPFflows worker --loglevel=info

4. Celery beat
    celery -A TGPFflows beat --loglevel=info

5. Django
    python manage.py runserver

Running tests (in-memory SQLite, MySQL does not need to be running):

    python manage.py test orders --settings=orders.test_settings

Scraper tests compare recorded browser action traces to golden files in orders/fixtures/scraper_traces/. After an intentional scraper change, rewrite them with:

    UPDATE_TRACES=1 python manage.py test orders.tests_scrapers --settings=orders.test_settings
