"""Business hours. WEEKDAY_CLOSE and SUNDAY_CLOSE are also used by the Celery beat schedule
(TGPFflows/settings.py) so the closing-time restock digest follows these hours."""

from datetime import datetime

_OPEN_HOUR = 10
_OPEN_MINUTE = 30  # earliest ETA start (gives driver time to get going)
WEEKDAY_CLOSE = 22  # 10pm Mon-Sat
SUNDAY_CLOSE = 17  # 5pm Sun


def closing_dt(ref: datetime) -> datetime:
    """Closing time on the day of `ref`."""
    close_hour = SUNDAY_CLOSE if ref.weekday() == 6 else WEEKDAY_CLOSE
    return ref.replace(hour=close_hour, minute=0, second=0, microsecond=0)


def clamp_to_opening(dt: datetime) -> datetime:
    """`dt`, or the earliest ETA start of its day if that is later."""
    earliest = dt.replace(hour=_OPEN_HOUR, minute=_OPEN_MINUTE, second=0, microsecond=0)
    return max(dt, earliest)


def is_past_closing(eta_start: datetime) -> bool:
    """Is this ETA at or after today's closing time? Such an order is offered a reschedule text."""
    return eta_start >= closing_dt(datetime.now())
