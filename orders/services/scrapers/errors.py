from contextlib import contextmanager


# Raised by a scraper when a step fails BEFORE its irreversible click (the
# message was not sent, the note not saved, the dispatch not confirmed...), so
# nothing happened on the outside and running the whole task again is safe. The
# Celery tasks retry these automatically. A failure at or after the irreversible
# click is never wrapped in this: it propagates as-is (tagged with `.step`) and
# is never retried automatically, because it may have gone through.
class BeforeCommitError(Exception):
    def __init__(self, step: str, cause: BaseException = None, detail: str = None):
        self.step = step
        self.cause = cause
        if detail is None:
            detail = describe(cause) if cause is not None else step
        super().__init__(detail)


# "ExcType: first line of the message" - Selenium messages end in a long
# stacktrace that is noise on a dashboard card
def describe(exc: BaseException, limit: int = 200) -> str:
    message = (str(exc).strip().splitlines() or [""])[0].strip()
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


# the step a failure happened in, if the scraper tagged it
def failed_step(exc: BaseException) -> str:
    return getattr(exc, "step", None) or "unknown"


# anything that goes wrong inside is a BeforeCommitError (safe to retry)
@contextmanager
def before_commit(step: str):
    try:
        yield
    except BeforeCommitError:
        raise
    except Exception as exc:
        raise BeforeCommitError(step, exc) from exc


# anything that goes wrong inside is re-raised as-is, tagged with its step
@contextmanager
def at_commit(step: str):
    try:
        yield
    except Exception as exc:
        if getattr(exc, "step", None) is None:
            exc.step = step
        raise


# Re-raise `exc` the right way for code that tracks whether it has committed
# something yet (the row/product loops). Before any commit the whole task is safe
# to retry, so this is a BeforeCommitError. After one it must never be retried
# automatically, so it is a plain error (a BeforeCommitError seen here is
# converted, because retrying would repeat what already went through).
def fail(step: str, exc: BaseException, committed: bool):
    if not committed:
        if isinstance(exc, BeforeCommitError):
            raise exc
        raise BeforeCommitError(step, exc) from exc
    if isinstance(exc, BeforeCommitError):
        plain = RuntimeError(describe(exc.cause) if exc.cause is not None else str(exc))
        plain.step = exc.step
        raise plain from exc
    if getattr(exc, "step", None) is None:
        exc.step = step
    raise exc
