import logging
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
from waldur_core.core.utils import broadcast_mail, format_homeport_link, pwgen
from waldur_core.structure import models as structure_models
from waldur_core.users import models, utils
from waldur_core.users.utils import generate_safe_username

logger = logging.getLogger(__name__)


@shared_task(name="waldur_core.users.cancel_expired_invitations")
def cancel_expired_invitations(invitations=None):
    """
    Invitation lifetime must be specified in Waldur Core settings with parameter
    "INVITATION_LIFETIME". If invitation creation time is less than expiration time, the invitation will set as expired.
    """
    expiration_date = timezone.now() - settings.WALDUR_CORE["INVITATION_LIFETIME"]
    if not invitations:
        invitations = models.Invitation.objects.filter(
            state=models.Invitation.State.PENDING
        )
    invitations = invitations.filter(created__lte=expiration_date)
    invitations.update(state=models.Invitation.State.EXPIRED)

    for invitation in invitations:
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

    context = utils.get_invitation_context(invitation, sender)
    context["link"] = utils.get_invitation_link(invitation_uuid)
    context["scope_link"] = utils.get_scope_link(
        context["type"], invitation.scope.uuid.hex
    )
    site_link = format_homeport_link()
    context["site_host"] = urlparse(site_link).hostname

    if settings.WALDUR_CORE["INVITATION_USE_WEBHOOKS"]:
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
    context = utils.get_invitation_context(invitation, sender)
    broadcast_mail(
        "users", "invitation_rejected", context, [invitation.created_by.email]
    )


@shared_task(name="waldur_core.users.send_reminder_for_pending_invitations")
def send_reminder_for_pending_invitations():
    expiration_date = (
        timezone.now() - settings.WALDUR_CORE["INVITATION_LIFETIME"] - timedelta(days=1)
    )
    pending_invitations = models.Invitation.objects.filter(
        state=models.Invitation.State.PENDING, created__lte=expiration_date
    )

    for invitation in pending_invitations:
        context = utils.get_invitation_context(invitation, invitation.created_by.email)
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


@shared_task(name="waldur_core.users.get_or_create_user")
def get_or_create_user(invitation_uuid, sender):
    invitation = models.Invitation.objects.get(uuid=invitation_uuid)

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
        sender = invitation.created_by.full_name or invitation.created_by.username
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


@shared_task(name="waldur_core.users.cancel_expired_group_invitations")
def cancel_expired_group_invitations():
    """
    Invitation lifetime must be specified in Waldur Core settings with parameter
    "GROUP_INVITATION_LIFETIME". If invitation creation time is less than expiration time,
    the invitation will set as expired.
    """
    expiration_date = timezone.now() - settings.WALDUR_CORE["GROUP_INVITATION_LIFETIME"]
    invitations = models.GroupInvitation.objects.filter(
        is_active=True,
        created__lte=expiration_date,
    )
    invitations.update(is_active=False)


@shared_task(
    name="waldur_core.users.send_mail_notification_about_permission_request_has_been_submitted"
)
def send_mail_notification_about_permission_request_has_been_submitted(
    permission_request_id,
):
    permission_request: models.PermissionRequest = models.PermissionRequest.objects.get(
        id=permission_request_id
    )
    requests_link = format_homeport_link("profile/permission-requests/")
    users = utils.get_users_for_notification_about_request_has_been_submitted(
        permission_request
    )
    emails = [u.email for u in users if u.email] if users else []

    if emails:
        broadcast_mail(
            "users",
            "permission_request_submitted",
            {"permission_request": permission_request, "requests_link": requests_link},
            emails,
        )


@shared_task(name="waldur_core.users.process_pending_project_invitations")
def process_pending_project_invitations():
    project_content_type = ContentType.objects.get_for_model(structure_models.Project)
    active_project_ids = structure_models.Project.objects.filter(
        start_date__lte=timezone.now()
    ).values_list("id", flat=True)
    invitations = models.Invitation.objects.filter(
        state=models.Invitation.State.PENDING_PROJECT,
        object_id__in=active_project_ids,
        content_type=project_content_type,
    )
    for invitation in invitations:
        invitation.state = models.Invitation.State.PENDING
        invitation.save()
        sender = invitation.created_by.full_name or invitation.created_by.username
        transaction.on_commit(
            lambda: process_invitation.delay(invitation.uuid.hex, sender)
        )
