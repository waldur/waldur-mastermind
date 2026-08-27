import logging

logger = logging.getLogger(__name__)

KEY = "proposal.access_request_state_changed"


def remove_access_request_notification(apps, schema_editor):
    """Fold the access-request wording back under one switch.

    It shipped briefly as a notification of its own. A deployment is in exactly
    one service access mode and so only ever sends one of the two wordings, which
    made the second switch inert wherever it appeared and left an operator two
    rows to reason about for a single event. The templates are now declared by
    ``proposal.proposal_state_changed`` and selected per deployment, so this row
    can no longer be reached from the registry.

    Only the Notification goes. The NotificationTemplate and dbtemplates rows for
    the access_request_* paths are still in use -- they belong to the surviving
    notification now -- so removing them would delete templates that are about to
    be sent, and any operator customisation of them along with it.
    """
    Notification = apps.get_model("core", "Notification")

    deleted, _ = Notification.objects.filter(key=KEY).delete()
    if deleted:
        logger.info("Removed notification '%s'; it is now a template variant.", KEY)
