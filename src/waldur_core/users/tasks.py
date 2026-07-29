import logging
import re
import traceback
from datetime import timedelta
from smtplib import SMTPException
from urllib.parse import urlparse

import requests
from celery import shared_task
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from python_freeipa import exceptions as freeipa_exceptions

from waldur_core.core import models as core_models
from waldur_core.core.utils import (
    broadcast_mail,
    format_homeport_link,
    is_robot_user,
    pwgen,
)
from waldur_core.structure import models as structure_models
from waldur_core.users import models, utils
from waldur_core.users.enums import InvitationState
from waldur_core.users.utils import generate_safe_username

logger = logging.getLogger(__name__)


def is_invitation_scope_unavailable(invitation: models.Invitation) -> bool:
    """Return True when invitation scope is missing or project is soft-deleted."""
    scope = invitation.scope
    if scope is None:
        return True
    return bool(getattr(scope, "is_removed", False))


@shared_task(name="waldur_core.users.cancel_expired_invitations")
def cancel_expired_invitations(invitations=None):
    """
    Invitation lifetime must be specified in Waldur Core settings with parameter
    "INVITATION_LIFETIME". If invitation creation time is less than expiration time, the invitation will set as expired.
    """
    now = timezone.now()
    if not invitations:
        invitations = models.Invitation.objects.filter(
            state__in=[InvitationState.PENDING, InvitationState.PENDING_PROJECT]
        )
    elif hasattr(invitations, "filter"):
        invitations = invitations.filter(
            state__in=[InvitationState.PENDING, InvitationState.PENDING_PROJECT]
        )
    else:
        invitations = [
            invitation
            for invitation in invitations
            if invitation.state
            in [InvitationState.PENDING, InvitationState.PENDING_PROJECT]
        ]

    expired_invitations = [
        invitation
        for invitation in invitations
        if invitation.get_expiration_time() <= now
    ]
    if not expired_invitations:
        return

    models.Invitation.objects.filter(
        uuid__in=[invitation.uuid for invitation in expired_invitations]
    ).update(state=InvitationState.EXPIRED)

    for invitation in expired_invitations:
        # Skip invitations where scope was deleted or soft-deleted
        if is_invitation_scope_unavailable(invitation):
            logger.warning(
                "Skipping expired invitation notification for %s: "
                "scope was deleted or terminated",
                invitation.uuid,
            )
            continue

        # Robot-created invitations would notify SITE_EMAIL,
        # producing a helpdesk ticket per expired invitation
        if is_robot_user(invitation.created_by):
            logger.info(
                "Skipping expired invitation notification for %s: "
                "created by robot account %s",
                invitation.uuid,
                invitation.created_by.username,
            )
            continue

        context = utils.get_invitation_context(invitation, invitation.created_by.email)

        logger.info(
            "About to send a notification about expired invitation to {email}. Invitation recipient was {user}".format(
                email=invitation.created_by.email, user=invitation.email, **context
            )
        )

        broadcast_mail(
            "users",
            "invitation_expired",
            context,
            [
                invitation.created_by.email,
            ],
        )


@shared_task(name="waldur_core.users.send_invitation_created")
def send_invitation_created(invitation_uuid, sender):
    """
    Invitation notification is sent to the user so that he can accept it and receive permissions.
    """
    invitation = models.Invitation.objects.get(uuid=invitation_uuid)

    if invitation.execution_state != models.Invitation.ExecutionState.PROCESSING:
        invitation.begin_processing()

    invitation.error_message = ""
    invitation.error_traceback = ""
    invitation.save(
        update_fields=["execution_state", "error_message", "error_traceback"]
    )

    # Check if scope still exists or has been soft-deleted (terminated)
    if is_invitation_scope_unavailable(invitation):
        error_msg = (
            f"Cannot send invitation {invitation_uuid}: "
            "the related scope has been deleted or terminated"
        )
        logger.error(error_msg)
        invitation.error_message = error_msg
        invitation.set_erred()
        invitation.save(update_fields=["error_message", "execution_state"])
        return

    context = utils.get_invitation_context(invitation, sender)
    context["link"] = utils.get_invitation_link(invitation_uuid)
    context["scope_link"] = utils.get_scope_link(
        context["type"], invitation.scope.uuid.hex
    )
    site_link = format_homeport_link()
    context["site_host"] = urlparse(site_link).hostname

    # Webhooks only support project invitations. For other scope types (customer,
    # organization, call), fall back to email.
    is_project_invitation = isinstance(invitation.scope, structure_models.Project)
    use_webhook = (
        settings.WALDUR_CORE["INVITATION_USE_WEBHOOKS"] and is_project_invitation
    )

    if use_webhook:
        webhook_url = settings.WALDUR_CORE["INVITATION_WEBHOOK_URL"]

        if webhook_url == "":
            logger.error("Webhook URL for invitations is blank, skipping processing")
            return

        logger.info(
            "About to send invitation to {url} for {email} to join {name} {type} as {role}".format(
                url=webhook_url, email=invitation.email, **context
            )
        )
        try:
            utils.post_invitation_to_url(webhook_url, context)

            invitation.set_ok()
            invitation.save(update_fields=["execution_state"])
        except Exception as exc:
            logger.error(exc)
            invitation.error_message = str(exc)
            invitation.error_traceback = traceback.format_exc()
            invitation.set_erred()
            invitation.save(
                update_fields=["error_message", "execution_state", "error_traceback"]
            )

    else:
        logger.info(
            "About to send invitation to {email} to join {name} {type} as {role}".format(
                email=invitation.email, **context
            )
        )
        try:
            broadcast_mail("users", "invitation_created", context, [invitation.email])

            invitation.set_ok()
            invitation.save(update_fields=["execution_state"])
        except SMTPException as e:
            invitation.error_message = str(e)
            invitation.set_erred()
            invitation.save(update_fields=["error_message", "execution_state"])
            raise


@shared_task(name="waldur_core.users.send_invitation_requested")
def send_invitation_requested(invitation_uuid, sender):
    """
    Invitation request is sent to staff users so that they can approve or reject invitation.
    """
    invitation = models.Invitation.objects.get(uuid=invitation_uuid)

    # Check if scope still exists or has been soft-deleted (terminated)
    if is_invitation_scope_unavailable(invitation):
        logger.error(
            "Cannot send invitation request %s: "
            "the related scope has been deleted or terminated",
            invitation_uuid,
        )
        return

    base_context = utils.get_invitation_context(invitation, sender)

    staff_users = (
        core_models.User.objects.filter(is_staff=True, is_active=True)
        .exclude(email="")
        .exclude(notifications_enabled=False)
    )
    for user in staff_users:
        token = utils.get_invitation_token(invitation, user)
        approve_link = format_homeport_link("invitation_approve/{token}/", token=token)
        reject_link = format_homeport_link("invitation_reject/{token}/", token=token)
        context = dict(
            approve_link=approve_link, reject_link=reject_link, **base_context
        )
        broadcast_mail("users", "invitation_requested", context, [user.email])


@shared_task(name="waldur_core.users.send_invitation_rejected")
def send_invitation_rejected(invitation_uuid, sender):
    """
    Invitation notification is sent to the user which has created invitation.
    """
    invitation = models.Invitation.objects.get(uuid=invitation_uuid)

    # Check if scope still exists or has been soft-deleted (terminated)
    if is_invitation_scope_unavailable(invitation):
        logger.error(
            "Cannot send invitation rejection %s: "
            "the related scope has been deleted or terminated",
            invitation_uuid,
        )
        return

    context = utils.get_invitation_context(invitation, sender)
    broadcast_mail(
        "users", "invitation_rejected", context, [invitation.created_by.email]
    )


@shared_task(name="waldur_core.users.send_reminder_for_pending_invitations")
def send_reminder_for_pending_invitations():
    """Send reminder emails for pending invitations that are about to expire."""
    now = timezone.now()
    pending_invitations = models.Invitation.objects.filter(
        state=InvitationState.PENDING
    )

    for invitation in pending_invitations:
        reminder_date = invitation.get_expiration_time() - timedelta(days=1)
        if reminder_date > now:
            continue
        # Skip invitations where scope was deleted or soft-deleted
        if is_invitation_scope_unavailable(invitation):
            logger.warning(
                "Skipping pending invitation reminder for %s: "
                "scope was deleted or terminated",
                invitation.uuid,
            )
            continue

        sender = (
            invitation.created_by.full_name
            or invitation.created_by.email
            or invitation.created_by.username
        )
        context = utils.get_invitation_context(invitation, sender)
        context["link"] = utils.get_invitation_link(invitation.uuid)
        site_link = format_homeport_link()
        context["site_host"] = urlparse(site_link).hostname
        context["reminder"] = True

        logger.info(
            "About to send a reminder about pending invitation to {email} to join {name} {type} as {role}".format(
                email=invitation.email, **context
            )
        )

        broadcast_mail(
            "users",
            "invitation_created",
            context,
            [
                invitation.email,
            ],
        )


def is_permanent_webhook_error(error_message: str) -> bool:
    """
    Check if the error message indicates a permanent failure that shouldn't be retried.

    HTTP 4xx errors (client errors) are typically permanent failures:
    - 400 Bad Request: The request is invalid
    - 401/403 Unauthorized/Forbidden: Authentication/authorization issues
    - 404 Not Found: Resource doesn't exist
    - 409 Conflict: Conflict with current state (e.g., user disabled)
    - 410 Gone: Resource no longer available

    HTTP 5xx errors (server errors) are typically temporary and can be retried.
    """
    if not error_message:
        return False

    # Match patterns like "has failed: 4XX" where XX is any two digits
    pattern = r"has failed: 4\d{2}"
    return bool(re.search(pattern, error_message))


@shared_task(name="waldur_core.users.resend_stuck_invitations")
def resend_stuck_invitations():
    """
    Reconcile stuck invitations that were never sent due to errors or broker/worker downtime.
    """
    expiration_date = timezone.now() - settings.WALDUR_CORE["INVITATION_LIFETIME"]

    stuck_invitations = models.Invitation.objects.filter(
        state=InvitationState.PENDING,
        created__gte=expiration_date,
        execution_state__in=[
            models.Invitation.ExecutionState.SCHEDULED,
            models.Invitation.ExecutionState.ERRED,
        ],
    )

    for invitation in stuck_invitations:
        # Skip invitations where scope was deleted or soft-deleted
        if is_invitation_scope_unavailable(invitation):
            logger.warning(
                "Skipping stuck invitation %s: scope was deleted or terminated",
                invitation.uuid,
            )
            continue

        # Skip invitations with permanent errors (4xx HTTP status codes)
        if is_permanent_webhook_error(invitation.error_message):
            logger.debug(
                "Skipping invitation %s due to permanent error: %s",
                invitation.uuid.hex,
                invitation.error_message,
            )
            continue

        sender = invitation.created_by and (
            invitation.created_by.full_name or invitation.created_by.username
        )
        process_invitation.delay(invitation.uuid.hex, sender)


@shared_task(name="waldur_core.users.get_or_create_user")
def get_or_create_user(invitation_uuid, sender):
    invitation = models.Invitation.objects.get(uuid=invitation_uuid)

    # Check if scope still exists or has been soft-deleted (terminated)
    if is_invitation_scope_unavailable(invitation):
        error_msg = (
            f"Cannot process invitation {invitation_uuid}: "
            "the related scope has been deleted or terminated"
        )
        logger.error(error_msg)
        invitation.error_message = error_msg
        invitation.set_erred()
        invitation.save(update_fields=["error_message", "execution_state"])
        return

    user, created = utils.get_or_create_user(invitation)
    username = generate_safe_username(user.username)
    password = pwgen()
    try:
        profile, created = utils.get_or_create_profile(user, username, password)
    except (freeipa_exceptions.FreeIPAError, requests.RequestException) as e:
        logger.exception(
            "Unable to create FreeIPA profile for user with ID: %s", user.id
        )
        invitation.error_message = str(e)
        invitation.save(update_fields=["error_message"])
        raise
    if created:
        sender = invitation.created_by and (
            invitation.created_by.full_name or invitation.created_by.username
        )
        context = utils.get_invitation_context(invitation, sender)
        context["username"] = username
        context["password"] = password
        context["link"] = utils.get_invitation_link(invitation_uuid)
        broadcast_mail("users", "invitation_approved", context, [invitation.email])
    else:
        send_invitation_created(invitation_uuid, sender)


@shared_task(name="waldur_core.users.process_invitation")
def process_invitation(invitation_uuid, sender):
    if settings.WALDUR_CORE["INVITATION_CREATE_MISSING_USER"]:
        get_or_create_user(invitation_uuid, sender)
    else:
        send_invitation_created(invitation_uuid, sender)


@shared_task(
    name="waldur_core.users.send_mail_notification_about_permission_request_has_been_submitted"
)
def send_mail_notification_about_permission_request_has_been_submitted(
    permission_request_id,
):
    permission_request = models.PermissionRequest.objects.get(id=permission_request_id)
    customer_uuid = permission_request.invitation.customer.uuid.hex
    requests_link = format_homeport_link(
        "organizations/{customer_uuid}/group-invitations/",
        customer_uuid=customer_uuid,
    )
    users = utils.get_users_for_notification_about_request_has_been_submitted(
        permission_request
    )
    customer = permission_request.invitation.customer
    emails = [u.email for u in users if u.email]
    emails += utils.get_customer_notification_emails(customer)
    # Fall back to staff when there are no approvers and no customer emails.
    if not any(email for email in emails):
        emails = [u.email for u in utils.get_staff_users_for_notification() if u.email]
    # Preserve order while dropping blanks and duplicates.
    emails = list(dict.fromkeys(email for email in emails if email))

    if emails:
        broadcast_mail(
            "users",
            "permission_request_submitted",
            {"permission_request": permission_request, "requests_link": requests_link},
            emails,
        )
    else:
        logger.warning(
            "No recipients found for permission request %s notification: "
            "customer %s (%s) has no owners with the create permission, no "
            "contact or notification emails, and no staff are available.",
            permission_request.uuid.hex,
            customer.name,
            customer.uuid.hex,
        )


@shared_task(
    name="waldur_core.users.send_mail_notification_about_permission_request_has_been_rejected"
)
def send_mail_notification_about_permission_request_has_been_rejected(
    permission_request_id,
):
    """Notify the requester that their permission request has been rejected."""
    permission_request = models.PermissionRequest.objects.get(id=permission_request_id)
    requester = permission_request.created_by

    if not requester.email:
        return

    broadcast_mail(
        "users",
        "permission_request_rejected",
        {"permission_request": permission_request},
        [requester.email],
    )


@shared_task(name="waldur_core.users.process_pending_project_invitations")
def process_pending_project_invitations():
    """Process project invitations for projects that have become active."""
    project_content_type = ContentType.objects.get_for_model(structure_models.Project)
    active_project_ids = structure_models.Project.objects.filter(
        start_date__lte=timezone.now()
    ).values_list("id", flat=True)
    invitations = models.Invitation.objects.filter(
        state=InvitationState.PENDING_PROJECT,
        object_id__in=active_project_ids,
        content_type=project_content_type,
    )
    for invitation in invitations:
        invitation.state = InvitationState.PENDING
        invitation.save()
        sender = invitation.created_by and (
            invitation.created_by.full_name or invitation.created_by.username
        )
        transaction.on_commit(
            lambda: process_invitation.delay(invitation.uuid.hex, sender)
        )
