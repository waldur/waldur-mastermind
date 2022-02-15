from django.conf import settings
from django.core.signing import BadSignature, TimestampSigner
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from python_freeipa import exceptions as freeipa_exceptions
from rest_framework import serializers

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.constants import get_domain_message
from waldur_core.core.utils import pwgen
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import CustomerRole, ProjectRole
from waldur_core.users import models
from waldur_freeipa import tasks
from waldur_freeipa.backend import FreeIPABackend
from waldur_freeipa.models import Profile
from waldur_freeipa.utils import generate_username


def get_invitation_context(invitation, sender):
    if invitation.project_role is not None:
        role_display = {
            ProjectRole.MANAGER: 'project manager',
            ProjectRole.MEMBER: 'project member',
            ProjectRole.ADMINISTRATOR: 'system administrator',
        }.get(invitation.project_role, invitation.get_project_role_display())
        context = dict(
            type=_('project'),
            name=invitation.project.name,
            role=_(get_domain_message(role_display)),
        )
    else:
        role_display = {
            CustomerRole.OWNER: 'organization owner',
        }.get(invitation.customer_role, invitation.get_customer_role_display())
        context = dict(
            type=_('organization'),
            name=invitation.customer.name,
            role=_(get_domain_message(role_display)),
        )

    context['sender'] = sender
    context['invitation'] = invitation
    return context


def get_invitation_token(invitation, user):
    signer = TimestampSigner()
    payload = '%s.%s' % (user.uuid.hex, invitation.uuid.hex)
    return signer.sign(payload)


def parse_invitation_token(token):
    signer = TimestampSigner()
    try:
        payload = signer.unsign(
            token, max_age=settings.WALDUR_CORE['INVITATION_MAX_AGE']
        )
    except BadSignature:
        raise serializers.ValidationError('Invalid signature.')

    parts = payload.split('.')
    if len(parts) != 2:
        raise serializers.ValidationError('Invalid payload.')

    user_uuid = parts[0]
    invitation_uuid = parts[1]

    if not core_utils.is_uuid_like(user_uuid):
        raise serializers.ValidationError('Invalid user UUID.')

    try:
        user = core_models.User.objects.filter(
            uuid=parts[0], is_active=True, is_staff=True
        ).get()
    except core_models.User.DoesNotExist:
        raise serializers.ValidationError('Invalid user UUID.')

    if not core_utils.is_uuid_like(invitation_uuid):
        raise serializers.ValidationError('Invalid invitation UUID.')

    try:
        invitation = models.Invitation.objects.get(
            uuid=parts[1], state=models.Invitation.State.REQUESTED
        )
    except models.Invitation.DoesNotExist:
        raise serializers.ValidationError('Invalid invitation UUID.')

    return user, invitation


def normalize_username(username):
    return ''.join(c if c.isalnum() else '_' for c in username.lower())


def generate_safe_username(username):
    username = generate_username(username)
    # Maximum length for FreeIPA username is 32 chars
    if len(username) > 32:
        prefix_length = len(settings.WALDUR_FREEIPA['USERNAME_PREFIX'])
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
            'full_name',
            'native_name',
            'organization',
            'civil_number',
            'job_title',
            'phone_number',
        )
    }
    user = core_models.User.objects.create_user(
        username=username,
        email=invitation.email,
        registration_method='FREEIPA',
        **payload
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
    return core_utils.format_homeport_link('invitation/{uuid}/', uuid=uuid)


def can_manage_invitation_with(user, customer, customer_role=None, project_role=None):
    if user.is_staff:
        return True

    is_owner = customer.has_user(user, structure_models.CustomerRole.OWNER)
    can_manage_owners = settings.WALDUR_CORE['OWNERS_CAN_MANAGE_OWNERS']

    # It is assumed that either customer_role or project_role is not None
    if customer_role:
        return is_owner and can_manage_owners
    if project_role:
        return is_owner


def get_users_for_notification_about_request_has_been_submitted(permission_request):
    can_manage_owners = settings.WALDUR_CORE['OWNERS_CAN_MANAGE_OWNERS']
    project_role = permission_request.invitation.project_role
    owners = permission_request.invitation.customer.get_owners()
    staff_users = (
        core_models.User.objects.filter(is_staff=True, is_active=True)
        .exclude(email='')
        .exclude(notifications_enabled=False)
    )

    if project_role:
        return owners
    elif can_manage_owners:
        return owners
    else:
        return staff_users
