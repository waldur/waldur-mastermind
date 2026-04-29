from django.db import transaction

from waldur_core.core.enums import ReviewStates
from waldur_core.users.models import PermissionRequest

from . import tasks


def create_notification_about_permission_request_has_been_submitted(
    sender, instance: PermissionRequest, created=False, **kwargs
):
    """Send a notification when a permission request has been submitted."""
    if created:
        return

    permission_request = instance

    if (
        not permission_request.tracker.has_changed("state")
        or not permission_request.state == ReviewStates.PENDING
    ):
        return

    transaction.on_commit(
        lambda: (
            tasks.send_mail_notification_about_permission_request_has_been_submitted.delay(
                permission_request.id
            )
        )
    )


def create_notification_about_permission_request_has_been_rejected(
    sender, instance: PermissionRequest, created=False, **kwargs
):
    """Notify the requester when their permission request has been rejected."""
    if created:
        return

    permission_request = instance

    if (
        not permission_request.tracker.has_changed("state")
        or permission_request.state != ReviewStates.REJECTED
    ):
        return

    transaction.on_commit(
        lambda: (
            tasks.send_mail_notification_about_permission_request_has_been_rejected.delay(
                permission_request.id
            )
        )
    )
