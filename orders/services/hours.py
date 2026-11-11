from datetime import datetime

_OPEN_HOUR = 10
_OPEN_MINUTE = 30   # earliest ETA start (gives driver time to get going)
WEEKDAY_CLOSE = 22  # 10pm Mon-Sat
SUNDAY_CLOSE = 17   # 5pm Sun


def closing_dt(ref: datetime) -> datetime:
    close_hour = SUNDAY_CLOSE if ref.weekday() == 6 else WEEKDAY_CLOSE
    return ref.replace(hour=close_hour, minute=0, second=0, microsecond=0)


def clamp_to_opening(dt: datetime) -> datetime:
    earliest = dt.replace(hour=_OPEN_HOUR, minute=_OPEN_MINUTE, second=0, microsecond=0)
    return max(dt, earliest)


def is_past_closing(eta_start: datetime) -> bool:
    return eta_start >= closing_dt(datetime.now())
