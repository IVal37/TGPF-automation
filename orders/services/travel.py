# imports from std lib
import logging
import time
from datetime import timedelta
from typing import Dict, List, Optional

# imports from third party
import requests
from django.conf import settings
from django.utils import timezone

# imports from project
from orders.models import TravelTime

logger = logging.getLogger(__name__)

_SAME_CITY_TRAVEL_TIME = 15
_DEFAULT_TRAVEL_TIME = 30

API_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
API_TIMEOUT = 3  # seconds: this can run inside a webhook, so never wait long
STALE_AFTER = timedelta(days=30)
RETRY_AFTER = 600  # seconds before a pair that just failed is asked about again
MAX_DESTINATIONS = 25  # Distance Matrix limit per request

# pairs that just failed -> time.monotonic() when they may be tried again. Keeps a
# Google outage from costing a 3-second wait on every route calculation.
_failed_until: Dict[tuple, float] = {}

# what a failed Google lookup can raise: network/HTTP trouble, a non-OK status or "no route"
# (ValueError), or an answer shaped differently than expected (KeyError/IndexError)
LOOKUP_ERRORS = (requests.RequestException, ValueError, KeyError, IndexError)


def reset_failure_memo() -> None:
    _failed_until.clear()


def _pair(city_a: str, city_b: str) -> tuple:
    return tuple(sorted((city_a, city_b)))


def api_key() -> str:
    return getattr(settings, "GOOGLE_MAPS_API_KEY", "") or ""


def needs_refresh(row: Optional[TravelTime]) -> bool:
    """A pair is worth asking Google about when we have no row, a typed-in number
    that Google never confirmed, or a Google number older than 30 days."""
    return row is None or row.fetched_at is None or timezone.now() - row.fetched_at > STALE_AFTER


# never let the API key into a log line (requests puts the full URL in its errors)
def _scrub(text: str) -> str:
    key = api_key()
    return text.replace(key, "***") if key else text


def _minutes_from_element(element: dict) -> Optional[int]:
    if element.get("status") != "OK":
        return None
    return max(1, round(element["duration"]["value"] / 60))


def fetch_minutes(origin: str, destinations: List[str]) -> Dict[str, int]:
    """Driving minutes from `origin` to each destination city via the Distance
    Matrix API (up to 25 per call). Returns only the destinations Google answered;
    raises on a transport/HTTP/API-level error."""
    response = requests.get(
        API_URL,
        params={
            "origins": f"{origin}, CA",
            "destinations": "|".join(f"{d}, CA" for d in destinations),
            "mode": "driving",
            "units": "imperial",
            "key": api_key(),
        },
        timeout=API_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "OK":
        raise ValueError(f"Distance Matrix status {data.get('status')!r}")
    elements = data["rows"][0]["elements"]
    result = {}
    for destination, element in zip(destinations, elements):
        minutes = _minutes_from_element(element)
        if minutes is not None:
            result[destination] = minutes
    return result


def store_google_minutes(city_a: str, city_b: str, minutes: int) -> None:
    a, b = _pair(city_a, city_b)
    TravelTime.objects.update_or_create(
        city_a=a,
        city_b=b,
        defaults={"minutes": minutes, "source": TravelTime.Source.GOOGLE, "fetched_at": timezone.now()},
    )


def get_travel_time(city_a: str, city_b: str) -> int:
    """Minutes to drive between two cities.

    Same city is always 15. Otherwise the stored TravelTime for the pair (either
    direction). With GOOGLE_MAPS_API_KEY set, a missing, never-confirmed or stale
    pair is refreshed from Google first. On any error, or with no key, the stored
    number is used (the seeded static table), and 30 for a pair we know nothing about."""
    if city_a == city_b:
        return _SAME_CITY_TRAVEL_TIME

    a, b = _pair(city_a, city_b)
    row = TravelTime.objects.filter(city_a=a, city_b=b).first()

    if api_key() and needs_refresh(row) and _failed_until.get((a, b), 0) <= time.monotonic():
        try:
            minutes = fetch_minutes(a, [b]).get(b)
            if minutes is None:
                raise ValueError("Google found no driving route")
        except LOOKUP_ERRORS as e:
            _failed_until[(a, b)] = time.monotonic() + RETRY_AFTER
            logger.warning(
                "get_travel_time: Google lookup for %s - %s failed, using the stored number: %s: %s",
                a,
                b,
                type(e).__name__,
                _scrub(str(e)),
            )
        else:
            store_google_minutes(a, b, minutes)
            return minutes

    return row.minutes if row is not None else _DEFAULT_TRAVEL_TIME
