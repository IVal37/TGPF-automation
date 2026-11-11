# imports from std lib
from datetime import datetime, timedelta


def parse_webhook_time(iso_str: str) -> datetime:
    """A WebJoint webhook timestamp (UTC, "Z"-suffixed) as the naive local wall-clock time
    used everywhere else (USE_TZ=False, hours.py, ...)."""
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def round_time(dt: datetime, minutes: int = 15, *, strict: bool = False) -> datetime:
    """Round `dt` up to the next multiple of `minutes` past the hour. A time already on a
    multiple stays put unless `strict`, which moves it to the next one."""
    interval_seconds = minutes * 60
    seconds_into_hour = dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
    add = (interval_seconds - (seconds_into_hour % interval_seconds)) % interval_seconds
    if strict and add == 0:
        add = interval_seconds
    return (dt + timedelta(seconds=add)).replace(second=0, microsecond=0)


def format_time(dt: datetime) -> str:
    """Clock time as customers see it in texts and notes: "2:30" (12-hour, no am/pm)."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"
