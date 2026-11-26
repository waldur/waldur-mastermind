"""Demo preset loads run in the background, one at a time.

The job table is the single source of truth for "a load is running": the web
workers that answer 503 meanwhile and the Celery worker that runs the load
are separate processes, and a deployment's default cache is per process, so
the cache can hold neither the lock nor the flag.
"""

import logging
import zlib
from datetime import timedelta

from celery import current_app
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from kombu import Queue

from waldur_mastermind.marketplace.models import DemoPresetLoad

logger = logging.getLogger(__name__)

States = DemoPresetLoad.States

# The load task is killed after this long, so a job still active past it (plus
# a margin for the queue) was abandoned -- typically a deploy restarting the
# worker mid-load.
LOAD_TIME_LIMIT = 60 * 60
STALE_AFTER = timedelta(seconds=LOAD_TIME_LIMIT + 5 * 60)

_LOCK_KEY = zlib.crc32(b"waldur.demo_preset_load")


class LoadInProgress(Exception):
    def __init__(self, job):
        super().__init__(f"Preset '{job.preset}' is already loading")
        self.job = job


def _stale_cutoff():
    return timezone.now() - STALE_AFTER


def _stale():
    cutoff = _stale_cutoff()
    return Q(state=States.PENDING, created__lt=cutoff) | Q(
        state=States.RUNNING, started_at__lt=cutoff
    )


def active_load():
    """The load that is pending or running, ignoring abandoned ones."""
    return (
        DemoPresetLoad.objects.filter(state__in=States.ACTIVE)
        .exclude(_stale())
        .only("uuid", "preset", "state")
        .first()
    )


def expire_stale_loads():
    """Mark abandoned loads erred, so they stop blocking new ones."""
    return (
        DemoPresetLoad.objects.filter(state__in=States.ACTIVE)
        .filter(_stale())
        .update(
            state=States.ERRED,
            finished_at=timezone.now(),
            error_message="The load did not finish in time; its worker was probably restarted.",
        )
    )


def start_load(preset, options, requested_by=""):
    """Create a pending load, or raise LoadInProgress if one is active.

    A transaction-scoped advisory lock serialises the check and the insert,
    so two concurrent requests cannot both start a load.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK_KEY])
        expire_stale_loads()
        current = active_load()
        if current:
            raise LoadInProgress(current)
        return DemoPresetLoad.objects.create(
            preset=preset, options=options, requested_by=requested_by
        )


def latest_load():
    expire_stale_loads()
    return DemoPresetLoad.objects.first()


def purge_task_queues():
    """Drop every task queued for this deployment's workers; return how many.

    Presets reuse fixed UUIDs, so a task queued before a reset would act on
    the object of the same UUID the load creates; others would fail against
    objects the load deleted, or run while the cleanup empties their tables.
    Tasks a worker has already taken are not affected.
    """
    purged = 0
    # Each queue gets a channel of its own, because a broker closes the
    # channel on which a missing queue was purged; closing the connection
    # closes them all.
    with current_app.connection_for_write() as connection_:
        for name in (
            settings.TASKS_QUEUE,
            settings.HEAVY_QUEUE,
            settings.BACKGROUND_QUEUE,
        ):
            queue = Queue(name, no_declare=True)(connection_.channel())
            try:
                purged += queue.purge() or 0
            except connection_.channel_errors:
                # Never declared: no worker has consumed it.
                logger.info("Task queue %s does not exist, nothing to purge", name)
    return purged


def dispatch(job, enqueue):
    """Clear the task queues for a cleanup load, then queue the load.

    Called once the job exists, so the API is already paused and no request
    queues a task in between. If the broker cannot be reached the job ends
    erred at once rather than pausing the API until it counts as abandoned.
    """
    try:
        if job.options.get("cleanup_first", True):
            purged = purge_task_queues()
            job.output = f"Dropped {purged} queued task(s) before the load.\n"
            job.save(update_fields=["output"])
        enqueue(job.uuid.hex)
    except Exception as e:
        logger.exception("Could not queue demo preset load %s", job.uuid.hex)
        DemoPresetLoad.objects.filter(pk=job.pk).update(
            state=States.ERRED,
            finished_at=timezone.now(),
            error_message=f"Could not queue the load: {e}",
        )
        raise
