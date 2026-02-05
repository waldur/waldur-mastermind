"""Event handlers for onboarding verification events."""

import logging

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

logger = logging.getLogger(__name__)


def log_verification_deleted(sender, instance, **kwargs):
    """
    Log when an onboarding verification is deleted.

    This handler captures verification data before deletion to preserve
    it in the event log. It's triggered by both manual deletions (via API)
    and automated cleanup tasks.
    """
    # Determine if this is a task-based deletion or manual deletion
    # Task deletions should have a specific marker or context
    deleted_by = getattr(instance, "_deleted_by", None)
    is_task_deletion = getattr(instance, "_deleted_by_task", False)

    if is_task_deletion:
        event_type = EventType.ONBOARDING_VERIFICATION_DELETED_BY_TASK
        message = (
            "Onboarding verification {verification_uuid} for {verification_legal_person_identifier} "
            "has been deleted by scheduled task."
        )
    else:
        event_type = EventType.ONBOARDING_VERIFICATION_DELETED
        if deleted_by:
            message = (
                "Onboarding verification {verification_uuid} for {verification_legal_person_identifier} "
                "has been deleted by user {deleted_by_username}."
            )
        else:
            message = (
                "Onboarding verification {verification_uuid} for {verification_legal_person_identifier} "
                "has been deleted."
            )

    event_context = {
        "verification": instance,
    }

    # Add deleted_by user info if available
    if deleted_by:
        event_context["deleted_by_username"] = deleted_by.username
        event_context["deleted_by_full_name"] = deleted_by.full_name
        event_context["deleted_by_uuid"] = deleted_by.uuid.hex

    event_logger.emit(
        message,
        event_type=event_type,
        event_context=event_context,
    )
