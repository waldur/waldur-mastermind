from django.db import transaction

from waldur_core.core.enums import ReviewStates
from waldur_core.users.models import PermissionRequest

from . import tasks


def create_notification_about_permission_request_has_been_submitted(
    sender, instance: PermissionRequest, created=False, **kwargs
):
    if created:
        return

    permission_request = instance

    if (
        not permission_request.tracker.has_changed("state")
        or not permission_request.state == ReviewStates.PENDING
    ):
        return

    transaction.on_commit(
        lambda: tasks.send_mail_notification_about_permission_request_has_been_submitted.delay(
            permission_request.id
        )
    )
