from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from waldur_core.core.enums import ReviewStates
from waldur_core.structure.models import Project
from waldur_core.users.enums import InvitationState
from waldur_core.users.models import GroupInvitation, Invitation, PermissionRequest

from . import tasks


def cancel_invitations_on_project_deletion(
    sender, instance: Project | None = None, **kwargs
):
    """Cancel open invitations scoped to a project when it is deleted (incl. soft delete)."""
    if instance is None:
        return

    project_ct = ContentType.objects.get_for_model(Project)
    Invitation.objects.filter(
        content_type=project_ct,
        object_id=instance.id,
        state__in=[
            InvitationState.PENDING,
            InvitationState.PENDING_PROJECT,
            InvitationState.REQUESTED,
        ],
    ).update(state=InvitationState.CANCELED)

    GroupInvitation.objects.filter(
        content_type=project_ct,
        object_id=instance.id,
        is_active=True,
    ).update(is_active=False)


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

    # Auto-approving invitations grant access immediately after submit() in the
    # same request (see GroupInvitationViewSet.submit_request), so there is
    # never anything for a Customer Owner to review here.
    if permission_request.invitation.auto_approve:
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
