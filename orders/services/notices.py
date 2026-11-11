# imports from std lib
import logging

from django.db import DatabaseError

from orders.models import Notice

logger = logging.getLogger(__name__)


# Adds a dashboard notice (a banner with a Dismiss button). Never raises: the
# things that report through it (startup, background tasks) must not fail
# because the notice could not be saved.
def add_notice(message: str, level: str = "info") -> None:
    try:
        Notice.objects.create(message=message, level=level)
    except DatabaseError as e:
        logger.error("could not save dashboard notice %r: %s", message, e)
