# imports from std lib
import logging

# imports from third party
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(text: str) -> None:
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.error("send_telegram_message: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return

    try:
        response = requests.post(
            _API_URL.format(token=token),
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        # Telegram's error responses carry a human-readable "description"
        # field (e.g. "Bad Request: chat not found") that requests' default
        # exception string doesn't include — log the raw body too.
        detail = getattr(e.response, "text", "") if getattr(e, "response", None) is not None else ""
        logger.error("send_telegram_message: failed to send: %s | response: %s", e, detail)
