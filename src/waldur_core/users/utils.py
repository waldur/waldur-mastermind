import logging

import requests
from django.conf import settings
from django.core.signing import BadSignature, TimestampSigner
from django.db import transaction
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import serializers

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.utils import pwgen
from waldur_core.permissions.utils import (
    get_create_permission,
    get_customer,
    get_users_with_permission,
    has_permission,
)
from waldur_core.users import models
from waldur_freeipa import tasks
from waldur_freeipa.backend import FreeIPABackend
from waldur_freeipa.models import Profile
from waldur_freeipa.utils import generate_username

logger = logging.getLogger(__name__)


def get_invitation_context(invitation: models.Invitation, sender):
    context = {"extra_invitation_text": invitation.extra_invitation_text}

    context.update(
        dict(
            type=invitation.scope._meta.verbose_name,
            name=invitation.scope.name,
            role=invitation.role.description,
        )
    )
    context["sender"] = sender
    context["invitation"] = invitation
    return context


def get_invitation_token(invitation, user):
    signer = TimestampSigner()
    payload = f"{user.uuid.hex}.{invitation.uuid.hex}"
    return signer.sign(payload)


def parse_invitation_token(token):
    signer = TimestampSigner()
    try:
        payload = signer.unsign(
            token, max_age=settings.WALDUR_CORE["INVITATION_MAX_AGE"]
        )
    except BadSignature:
        raise serializers.ValidationError("Invalid signature.")

    parts = payload.split(".")
    if len(parts) != 2:
        raise serializers.ValidationError("Invalid payload.")

    user_uuid = parts[0]
    invitation_uuid = parts[1]

    if not core_utils.is_uuid_like(user_uuid):
        raise serializers.ValidationError("Invalid user UUID.")

    try:
        user = core_models.User.objects.filter(
            uuid=parts[0], is_active=True, is_staff=True
        ).get()
    except core_models.User.DoesNotExist:
        raise serializers.ValidationError("Invalid user UUID.")

    if not core_utils.is_uuid_like(invitation_uuid):
        raise serializers.ValidationError("Invalid invitation UUID.")

    try:
        invitation = models.Invitation.objects.get(
            uuid=parts[1], state=models.Invitation.State.REQUESTED
        )
    except models.Invitation.DoesNotExist:
        raise serializers.ValidationError("Invalid invitation UUID.")

    return user, invitation


def normalize_username(username):
    return "".join(c if c.isalnum() else "_" for c in username.lower())


def generate_safe_username(username):
    username = generate_username(username)
    # Maximum length for FreeIPA username is 32 chars
    if len(username) > 32:
        prefix_length = len(settings.WALDUR_FREEIPA["USERNAME_PREFIX"])
        username = generate_username(pwgen(32 - prefix_length))
    return username


@transaction.atomic
def get_or_create_user(invitation):
    if invitation.civil_number:
        user = core_models.User.objects.filter(
            civil_number=invitation.civil_number
        ).first()
        if user:
            return user, False

    user = core_models.User.objects.filter(email=invitation.email).first()
    if user:
        return user, False

    username = normalize_username(invitation.email)
    user = core_models.User.objects.filter(username=username).first()
    if user:
        return user, False

    payload = {
        field: getattr(invitation, field)
        for field in (
            "full_name",
            "native_name",
            "organization",
            "civil_number",
            "job_title",
            "phone_number",
        )
    }
    user = core_models.User.objects.create_user(
        username=username,
        email=invitation.email,
        registration_method="FREEIPA",
        **payload,
    )
    user.set_unusable_password()
    user.save()
    return user, True


@transaction.atomic
def get_or_create_profile(user, username, password):
    profile = Profile.objects.filter(user=user).first()
    if profile:
        return profile, False

    profile = Profile.objects.create(
        user=user,
        username=username,
    )
    try:
        FreeIPABackend().create_profile(profile, password=password)
        tasks.schedule_sync()
    except freeipa_exceptions.DuplicateEntry:
        pass
    return profile, True


def get_invitation_link(uuid):
    return core_utils.format_homeport_link("invitation/{uuid}/", uuid=uuid)


def can_manage_invitation_with(request, scope):
    if request.user.is_staff:
        return True

    permission = get_create_permission(scope)
    if not permission:
        return False

    if has_permission(request, permission, scope):
        return True

    customer = get_customer(scope)
    if has_permission(request, permission, customer):
        return True

    return False


def get_users_for_notification_about_request_has_been_submitted(
    permission_request: models.PermissionRequest,
):
    staff_users = (
        core_models.User.objects.filter(is_staff=True, is_active=True)
        .exclude(email="")
        .exclude(notifications_enabled=False)
    )

    scope = permission_request.invitation.scope

    permission = get_create_permission(scope)
    if not permission:
        return staff_users

    users = get_users_with_permission(scope, permission)
    customer = get_customer(scope)
    if customer != scope:
        users |= get_users_with_permission(customer, permission)

    users = users.exclude(email="").exclude(notifications_enabled=False)

    return users or staff_users


def post_invitation_to_url(url: str, invitation: models.Invitation):
    payload = {
        "email": invitation.email,
        "role_name": invitation.role.name,
        "role_description": invitation.role.description,
        "scope_type": invitation.scope,
        "scope_name": invitation.scope.name,
        "scope_uuid": invitation.scope.uuid,
        "extra_invitation_text": invitation.extra_invitation_text,
        "created_by_full_name": invitation.created_by.full_name,
        "created_by_username": invitation.created_by.username,
        "expires": invitation.get_expiration_time(),
    }
    response = requests.post(url, json=payload)

    if response.status_code >= 200 and response.status_code < 300:
        logger.info("Invitation has been successfully send to %s", url)
    else:
        logger.warning(
            "Invitation sending has failed: %s, %s", response.status_code, response.text
        )
