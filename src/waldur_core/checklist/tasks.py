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


@shared_task(name="waldur_core.checklist.recompute_checklist_completions")
def recompute_checklist_completions(checklist_id):
    """Recompute completion of every completion of a checklist whose questions
    or dependencies changed.

    Only the two flags are written, and only where they change, so that other
    fields, such as a review saved meanwhile, and ``modified`` stay as they are.
    """
    checklist = models.Checklist.objects.filter(pk=checklist_id).first()
    if checklist is None:
        return
    questions = list(checklist.get_visibility_questions())
    for completion in models.ChecklistCompletion.objects.filter(
        checklist=checklist
    ).select_related("checklist"):
        status = completion.get_completion_status(questions)
        if status != (completion.is_completed, completion.requires_review):
            is_completed, requires_review = status
            models.ChecklistCompletion.objects.filter(pk=completion.pk).update(
                is_completed=is_completed, requires_review=requires_review
            )
