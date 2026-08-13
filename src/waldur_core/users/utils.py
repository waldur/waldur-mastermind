import logging

import requests
from constance import config
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.signing import BadSignature, TimestampSigner
from django.db import transaction
from django.db.models import Q
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import serializers

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.utils import pwgen
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import (
    get_create_permission,
    get_customer,
    get_users_with_permission,
    has_permission,
)
from waldur_core.users import models
from waldur_core.users.enums import InvitationState
from waldur_freeipa import tasks
from waldur_freeipa.backend import FreeIPABackend
from waldur_freeipa.models import Profile
from waldur_freeipa.utils import generate_username

logger = logging.getLogger(__name__)


def get_invitation_context(invitation: models.Invitation, sender):
    context = {"extra_invitation_text": invitation.extra_invitation_text}

    if invitation.scope is None:
        raise ValueError(
            f"Invitation {invitation.uuid} has no scope - "
            "the related object may have been deleted"
        )

    context.update(
        dict(
            type=str(
                invitation.scope._meta.verbose_name
            ),  # convert from proxy object to real string
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
            uuid=parts[1], state=InvitationState.REQUESTED
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
        prefix_length = len(config.FREEIPA_USERNAME_PREFIX)
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

    # Create user with minimal data - profile fields should come from IdP or self-assertion,
    # not from invitation. Invitation fields are for email personalization only.
    user = core_models.User.objects.create_user(
        username=username,
        email=invitation.email,
        registration_method="FREEIPA",
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


def get_scope_link(scope_type, scope_uuid):
    scope_to_homeport_prefix_map = {
        "project": "projects",
        "organization": "organizations",
        "call": "calls",
        "resource": "resources",
        "resource project": "resource-projects",
        # Offerings are a valid invitation scope (see TYPE_MAP). The
        # provider-side offering page is nested under its customer, so link to
        # the public offering route, which is keyed by the offering uuid alone.
        "offering": "marketplace-public-offering",
    }
    # ``scope_type`` is the model's verbose_name and its capitalization is not
    # consistent across models — Customer declares "organization" while
    # Offering declares "Offering". Match case-insensitively so a capitalized
    # verbose_name does not silently fall through to /unknown/.
    api_suffix = scope_to_homeport_prefix_map.get(str(scope_type).lower(), "unknown")
    if api_suffix == "unknown":
        logger.warning(
            "No HomePort route is mapped for invitation scope type %s; "
            "the notification will contain a dead scope link.",
            scope_type,
        )
    return core_utils.format_homeport_link(
        "{api_suffix}/{scope_uuid}/", api_suffix=api_suffix, scope_uuid=scope_uuid
    )


def can_manage_invitation_with(request, scope):
    # Check if the scope is a soft-deleted project
    if scope._meta.model_name == "project" and getattr(scope, "is_removed", False):
        return False

    if request.user.is_staff:
        return True

    permission = get_create_permission(scope)
    if not permission:
        return False

    if has_permission(request, permission, scope):
        return True

    # Walk up to the parent project (e.g. Resource → Project) so project
    # admins/managers can invite into resources within their project.
    project = getattr(scope, "project", None)
    if project is not None and has_permission(request, permission, project):
        return True

    customer = get_customer(scope)
    if has_permission(request, permission, customer):
        return True

    # Also allow users who have authority over the customer org itself (e.g. CUSTOMER.OWNER)
    # to manage invitations for any nested scope (project, offering, etc.).
    if customer is not scope:
        customer_permission = get_create_permission(customer)
        if customer_permission and has_permission(
            request, customer_permission, customer
        ):
            return True

    # In the call scope, to allow call_organizer role to manage invitation, we have to set permission scope to callmanagingorganisation
    if scope._meta.model_name == "call" and customer.callmanagingorganisation:
        if has_permission(request, permission, customer.callmanagingorganisation):
            return True

    return False


def can_manage_permission_request(request, invitation):
    # Approving an auto_create_project invitation creates a project and grants a
    # PROJECT role on it (see PermissionRequest.approve), so the relevant authority
    # is project creation within the customer rather than customer membership
    # management. Without this, a customer role that has CREATE_PROJECT_PERMISSION
    # but not CREATE_CUSTOMER_PERMISSION is offered the approve action in the UI
    # yet receives a 404 from the API.
    if invitation.auto_create_project:
        if request.user.is_staff:
            return True
        return has_permission(
            request,
            PermissionEnum.CREATE_PROJECT_PERMISSION,
            invitation.customer,
        )
    return can_manage_invitation_with(request, invitation.scope)


def get_invitation_duplicates(scope, invitations):
    if not invitations:
        return []

    pair_conditions = Q()
    for item in invitations:
        pair_conditions |= Q(email__iexact=item["email"], role=item["role"])

    pending_states = [
        InvitationState.PENDING,
        InvitationState.PENDING_PROJECT,
        InvitationState.REQUESTED,
    ]
    existing_invitations = (
        models.Invitation.objects.filter(
            content_type=ContentType.objects.get_for_model(scope),
            object_id=scope.id,
            state__in=pending_states,
        )
        .filter(pair_conditions)
        .order_by("created")
    )

    existing_by_pair = {}
    for invitation in existing_invitations:
        key = (invitation.email.lower(), invitation.role.uuid)
        if key not in existing_by_pair:
            existing_by_pair[key] = invitation

    duplicates = []
    added = set()
    seen = set()
    for item in invitations:
        key = (item["email"].lower(), item["role"].uuid)
        is_request_duplicate = key in seen
        seen.add(key)
        if key in added:
            continue
        if key in existing_by_pair or is_request_duplicate:
            existing = existing_by_pair.get(key)
            duplicates.append(
                {
                    "email": item["email"],
                    "role": item["role"].uuid,
                    "existing_invitation_uuid": (
                        existing.uuid if existing is not None else None
                    ),
                }
            )
            added.add(key)

    return duplicates


def get_users_for_notification_about_request_has_been_submitted(
    permission_request: models.PermissionRequest,
):
    invitation = permission_request.invitation

    # Notify the users who are actually authorized to approve the request. This
    # must mirror can_manage_permission_request: for an auto_create_project
    # invitation approval creates a project and grants a project role within the
    # customer, so the relevant authority is project creation within the customer
    # (typically the customer owners), NOT customer creation. Resolving the
    # permission from the scope type via get_create_permission() would yield
    # CREATE_CUSTOMER_PERMISSION for a customer-scoped invitation, which no owner
    # holds, leaving the recipient list empty.
    if invitation.auto_create_project:
        users = get_users_with_permission(
            invitation.customer, PermissionEnum.CREATE_PROJECT_PERMISSION
        )
    else:
        scope = invitation.scope
        permission = get_create_permission(scope)
        if not permission:
            return core_models.User.objects.none()

        users = get_users_with_permission(scope, permission)
        customer = get_customer(scope)
        if customer != scope:
            users |= get_users_with_permission(customer, permission)

    return users.exclude(email="").exclude(notifications_enabled=False)


def get_staff_users_for_notification():
    return (
        core_models.User.objects.filter(is_staff=True, is_active=True)
        .exclude(email="")
        .exclude(notifications_enabled=False)
    )


def get_customer_notification_emails(customer):
    """Return the customer's contact email plus its configured notification emails."""
    emails = []
    if customer.email:
        emails.append(customer.email)
    if customer.notification_emails:
        emails += [
            email.strip()
            for email in customer.notification_emails.split(",")
            if email.strip()
        ]
    return emails


def post_invitation_to_url(url: str, context):
    token_url = settings.WALDUR_CORE["INVITATION_WEBHOOK_TOKEN_URL"]
    client_id = settings.WALDUR_CORE["INVITATION_WEBHOOK_TOKEN_CLIENT_ID"]
    client_secret = settings.WALDUR_CORE["INVITATION_WEBHOOK_TOKEN_SECRET"]

    token_request_headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    token_params = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    token_response = requests.post(
        token_url, data=token_params, headers=token_request_headers
    )

    if token_response.status_code != 200:
        raise RuntimeError(
            f"Unable to fetch access token to send invitation, url: {token_url}, code {token_response.status_code}, body: {token_response.text}"
        )

    logger.info("Successfully fetched an access token from %s", token_url)

    access_token = token_response.json()["access_token"]

    invitation: models.Invitation = context["invitation"]
    invitation_payload = {
        "email": invitation.email,
        "role_name": invitation.role.name,
        "role_description": context["role"],
        "scope_type": context["type"],
        "scope_name": context["name"],
        "scope_uuid": invitation.scope.uuid.hex,
        "scope_slug": getattr(invitation.scope, "slug", ""),
        "extra_invitation_text": context["extra_invitation_text"],
        "created_by_full_name": invitation.created_by.full_name,
        "created_by_username": invitation.created_by.username,
        "expires": invitation.get_expiration_time().isoformat(),
        "redirect_scope_url": context["scope_link"],
        "redirect_url": context["link"],
        "invitation_uuid": invitation.uuid.hex,
    }
    invitation_headers = {"Authorization": f"Bearer {access_token}"}
    invitation_response = requests.post(
        url, json=invitation_payload, headers=invitation_headers
    )

    if 200 <= invitation_response.status_code < 300:
        logger.info("Invitation has been successfully sent to %s", url)
    else:
        raise RuntimeError(
            f"Invitation sending has failed: {invitation_response.status_code}, {invitation_response.text}"
        )
