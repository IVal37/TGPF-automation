# imports from std lib
import hmac
import logging
import re
from functools import wraps
from typing import Any, Callable

# imports from django
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden

logger = logging.getLogger(__name__)

# the three routes WebJoint calls; each is served at hooks/<WEBHOOK_SECRET>/<name>
HOOK_NAMES = ("new_order", "complete_order", "cancel_order")

_KEY_IN_PATH = re.compile(r"/hooks/[^/\s]+/")


# the secret rides in the URL path, so anything that prints a URL or request
# line (runserver's access log, our own log lines) must mask it first
def redact_webhook_key(text: str) -> str:
    return _KEY_IN_PATH.sub("/hooks/<redacted>/", text)


class RedactWebhookKeyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_webhook_key(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


# full webhook URLs to register in WebJoint for a given public base URL
def webhook_urls(base_url: str) -> dict:
    base = base_url.rstrip("/")
    return {name: f"{base}/hooks/{settings.WEBHOOK_SECRET}/{name}" for name in HOOK_NAMES}


def client_ip(request: HttpRequest) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "?")


# wraps a webhook view so it only runs when the key in the URL path matches
# WEBHOOK_SECRET; the wrapped view itself still takes just (request)
def require_webhook_key(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view)
    def wrapper(request: HttpRequest, key: str, *args: Any, **kwargs: Any) -> HttpResponse:
        # compare_digest on bytes handles non-ASCII input and runs in constant time
        if not hmac.compare_digest(key.encode(), settings.WEBHOOK_SECRET.encode()):
            logger.warning("webhook rejected: wrong key for %s from %s", view.__name__, client_ip(request))
            return HttpResponseForbidden()
        return view(request, *args, **kwargs)
    return wrapper
