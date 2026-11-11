# imports from std lib
import atexit
import subprocess
import time

# imports from third party
import requests

# imports from django
from django.conf import settings
from django.core.management.base import BaseCommand

# imports from project
from orders.services.scrapers.webjoint import set_webhooks


NGROK_API_TIMEOUT = 10  # seconds
# ngrok needs a few seconds before its API lists the new tunnel
TUNNEL_STARTUP_SECONDS = 5


def start_ngrok():
    ngrok_process = subprocess.Popen(
        ["ngrok", "http", "8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    atexit.register(lambda: (ngrok_process.poll() is None) and ngrok_process.terminate())

    time.sleep(TUNNEL_STARTUP_SECONDS)

    url = get_endpoint_link()
    set_webhooks(url)
    return url


def get_endpoint_link() -> str:
    token = settings.NGROK_AUTH_TOKEN
    if not token:
        raise ValueError("NGROK_AUTH_TOKEN is not set in .env")
    url = "https://api.ngrok.com/endpoints"
    headers = {
        "ngrok-version": "2",
        "Authorization": f"Bearer {token}",
    }
    response = requests.get(url, headers=headers, timeout=NGROK_API_TIMEOUT)
    response.raise_for_status()
    endpoints = response.json()["endpoints"]
    if not endpoints:
        raise ValueError("ngrok reports no running endpoint (did the tunnel start?)")
    return endpoints[0]["public_url"]


class Command(BaseCommand):
    help = 'Start ngrok tunnel'

    def handle(self, *args, **options):
        start_ngrok()
