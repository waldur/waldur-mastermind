"""Error answers of the appservice endpoints over the last hour, for diagnostics.

The counts live in the Django cache, which every API process shares (the
packaged deployments keep it in the database), so they cover all workers, and
an answer without an error writes nothing. One key per status class and
five-minute bucket keeps the hour rolling: an error is counted for at least an
hour, and for up to one bucket longer.

On the database cache an increment is a read and then a write, so errors in
several processes at the same moment can be undercounted, but never to zero:
the count says whether the homeserver's calls fail, not exactly how often.
"""

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 3600
BUCKET_SECONDS = 300
STATUS_CLASSES = ("4xx", "5xx")


def _key(status_class, bucket):
    return f"matrix_webhook_errors:{status_class}:{bucket}"


def _current_bucket():
    return int(timezone.now().timestamp()) // BUCKET_SECONDS


def record(status_code):
    """Count an answer if it is a 4xx or 5xx."""
    if status_code < 400:
        return
    status_class = "5xx" if status_code >= 500 else "4xx"
    key = _key(status_class, _current_bucket())
    timeout = WINDOW_SECONDS + BUCKET_SECONDS
    try:
        # Incremented first: on the database cache, adding an entry that is
        # already there is an INSERT the database refuses and logs.
        try:
            cache.incr(key)
        except ValueError:
            # The bucket's first error, unless another process adds the entry
            # at the same moment.
            if not cache.add(key, 1, timeout=timeout):
                cache.incr(key)
        # The database cache's incr rewrites the entry with the cache's default
        # timeout, five minutes, which would drop the bucket inside the hour.
        cache.touch(key, timeout)
    except Exception:
        # Counting must not turn an answer into an error, nor replace the
        # exception being raised.
        logger.warning("Could not count a Matrix appservice error", exc_info=True)


def counts():
    """``{"4xx": n, "5xx": n}`` over the last hour."""
    current = _current_bucket()
    # The current bucket has only begun, so a full hour needs the twelve
    # before it as well.
    buckets = range(current - WINDOW_SECONDS // BUCKET_SECONDS, current + 1)
    keys = {
        _key(status_class, bucket): status_class
        for status_class in STATUS_CLASSES
        for bucket in buckets
    }
    result = dict.fromkeys(STATUS_CLASSES, 0)
    for key, value in cache.get_many(list(keys)).items():
        result[keys[key]] += value
    return result
