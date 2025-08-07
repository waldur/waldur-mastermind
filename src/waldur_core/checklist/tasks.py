"""Celery tasks for checklist maintenance."""

import logging

from celery import shared_task

from . import models

logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.checklist.cleanup_orphaned_answers")
def cleanup_orphaned_answers():
    """
    Clean up Answer objects that have null completion field.

    This should not happen in normal operation as domain apps are expected
    to always set the completion field, but this task provides a safety net
    to clean up any orphaned answers that might exist.
    """
    orphaned_answers = models.Answer.objects.filter(completion__isnull=True)
    count = orphaned_answers.count()

    if count > 0:
        logger.warning(
            "Found %d orphaned Answer objects with null completion field, deleting them",
            count,
        )
        orphaned_answers.delete()
        logger.info("Successfully deleted %d orphaned Answer objects", count)
    else:
        logger.debug("No orphaned Answer objects found")

    return f"Cleaned up {count} orphaned Answer objects"
