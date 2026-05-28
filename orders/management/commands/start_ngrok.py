# imports from std lib
import os
import subprocess
import atexit
import requests
import time
import json

# imports from django
from django.core.management.base import BaseCommand

# imports from project
from orders.services.scrapers.webjoint import set_webhooks


def start_ngrok():
    ngrok_process = subprocess.Popen(
        ["ngrok", "http", "8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    atexit.register(lambda: (ngrok_process.poll() is None) and ngrok_process.terminate())

    time.sleep(5)

    url = get_endpoint_link()
    set_webhooks(url)


def get_endpoint_link() -> str:
    token = os.environ.get('NGROK_AUTH_TOKEN')
    if not token:
        raise ValueError("NGROK_AUTH_TOKEN is not set in .env")
    url = "https://api.ngrok.com/endpoints"
    headers = {
        "ngrok-version": "2",
        "Authorization": f"Bearer {token}",
    }
    response = requests.get(url, headers=headers)
    response_dict = json.loads(response.text)
    return response_dict["endpoints"][0]["public_url"]


class Command(BaseCommand):
    help = 'Start ngrok tunnel'

    def handle(self, *args, **options):
        start_ngrok()
