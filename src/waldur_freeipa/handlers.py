import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from python_freeipa import exceptions as freeipa_exceptions

from waldur_core.core import utils as core_utils
from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.models import SshPublicKey, User
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.quotas.models import QuotaLimit
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils

from . import backend, models, tasks, utils
from .models import Profile

logger = logging.getLogger(__name__)


def schedule_sync(*args, **kwargs):
    """Schedule a synchronization task, unless side effects are switched
    off: a change made in a transaction that is rolled back -- a workflow
    dry run, say -- is nothing to synchronise."""
    if get_skip_side_effects():
        return
    tasks.schedule_sync()


def schedule_sync_on_quota_change(
    sender, instance: QuotaLimit, created=False, **kwargs
):
    """Schedule a synchronization task when a quota is changed."""
    if instance.name != utils.QUOTA_NAME:
        return
    if created and instance.value == -1:
        return
    tasks.schedule_sync()


def log_profile_event(sender, instance: Profile, created=False, **kwargs):
    """Log FreeIPA profile creation, enable, and disable events."""
    profile = instance

    if created:
        event_logger.emit(
            "{username} FreeIPA profile has been created.",
            event_type=EventType.FREEIPA_PROFILE_CREATED,
            event_context={
                "user": profile.user,
                "username": profile.username,
            },
            scopes=[profile.user],
        )

    elif profile.tracker.has_changed("is_active") and profile.tracker.previous(
        "is_active"
    ):
        event_logger.emit(
            "{username} FreeIPA profile has been disabled.",
            event_type=EventType.FREEIPA_PROFILE_DISABLED,
            event_context={
                "user": profile.user,
                "username": profile.username,
            },
            scopes=[profile.user],
        )

    elif profile.tracker.has_changed("is_active") and not profile.tracker.previous(
        "is_active"
    ):
        event_logger.emit(
            "{username} FreeIPA profile has been enabled.",
            event_type=EventType.FREEIPA_PROFILE_ENABLED,
            event_context={
                "user": profile.user,
                "username": profile.username,
            },
            scopes=[profile.user],
        )


def log_profile_deleted(sender, instance: Profile, **kwargs):
    """Log FreeIPA profile deletion events."""
    profile = instance
    event_logger.emit(
        "{username} FreeIPA profile has been deleted.",
        event_type=EventType.FREEIPA_PROFILE_DELETED,
        event_context={
            "user": profile.user,
            "username": profile.username,
        },
        scopes=[profile.user],
    )


def schedule_ssh_key_sync_when_key_is_created(
    sender, instance: SshPublicKey, created=False, **kwargs
):
    """Schedule an SSH key synchronization task when a key is created."""
    if created:
        schedule_ssh_key_sync(instance)


def schedule_ssh_key_sync_when_key_is_deleted(sender, instance: SshPublicKey, **kwargs):
    """Schedule an SSH key synchronization task when a key is deleted."""
    schedule_ssh_key_sync(instance)


def schedule_ssh_key_sync(ssh_key):
    """Schedule an SSH key synchronization task."""
    try:
        profile = models.Profile.objects.get(user=ssh_key.user)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping SSH key synchronization because "
            "FreeIPA profile does not exist. "
            "User ID: %s",
            ssh_key.user.id,
        )
    else:
        transaction.on_commit(lambda: tasks.sync_profile_ssh_keys.delay(profile.pk))


def update_user(sender, instance: User, created=False, **kwargs):
    """Update a user's FreeIPA profile when their user account is updated."""
    user = instance

    if created:
        return

    if set(user.tracker.changed()) & {"is_active"}:
        try:
            profile = models.Profile.objects.get(user=user)
        except models.Profile.DoesNotExist:
            logger.debug(f"No FreeIPA profile found for user {user.username}.")
            return

        if user.is_active != profile.is_active:
            profile.is_active = user.is_active
            profile.save()

            if profile.is_active:
                logger.info(f"Activating user {profile.username} in FreeIPA.")
                tasks.user_enable.delay(core_utils.serialize_instance(profile))
            else:
                logger.info(f"Deactivating user {profile.username} in FreeIPA.")
                tasks.user_disable.delay(core_utils.serialize_instance(profile))

    elif set(user.tracker.changed()) & {
        "first_name",
        "last_name",
        "email",
        "organization",
        "job_title",
        "preferred_language",
        "phone_number",
    }:
        try:
            profile = models.Profile.objects.get(is_active=True, user=user)
        except models.Profile.DoesNotExist:
            return

        transaction.on_commit(
            lambda: tasks.update_user.delay(core_utils.serialize_instance(profile))
        )


@transaction.atomic
def get_or_create_profile(user, username, password):
    profile = models.Profile.objects.filter(user=user).first()
    if profile:
        return profile, False

    profile = models.Profile.objects.create(
        user=user,
        username=username,
    )
    try:
        backend.FreeIPABackend().create_profile(profile, password=password)
        tasks.schedule_sync()
    except freeipa_exceptions.DuplicateEntry:
        pass
    return profile, True


def resolve_freeipa_username(user) -> str:
    """Return the FreeIPA username for ``user``, or empty string if absent."""
    profile = models.Profile.objects.filter(user=user).first()
    if profile is None:
        logger.warning("There is no FreeIPA profile for user %s", user)
        return ""
    return profile.username


def update_offering_user_username_after_freeipa_profile_update(
    sender, instance: Profile, created=False, **kwargs
):
    """
    Update offering user usernames after FreeIPA profile creation/update.

    This handler is triggered when a user creates or updates their FreeIPA profile.
    It updates the username for all OfferingUsers that use FreeIPA username generation policy.

    Important: We call save() without update_fields to ensure the full save() method runs,
    which includes the state transition logic that sets state to OK when username is available.
    """
    profile = instance

    if not profile.tracker.has_changed("username") or not created:
        return

    # The policy may be set on the offering or defaulted by its provider, and
    # resolve_account_setting is Python-side, so the filter selects a superset
    # -- either names it -- and the loop confirms per row. A plain filter on
    # plugin_options would miss every offering that inherits the policy from
    # its provider, which is the gap provider account options introduced.
    policy = marketplace_utils.UsernameGenerationPolicy.FREEIPA.value
    offering_users = marketplace_models.OfferingUser.objects.filter(
        Q(offering__plugin_options__username_generation_policy=policy)
        | Q(
            offering__customer__serviceprovider__account_options__username_generation_policy=policy
        ),
        user=profile.user,
        is_restricted=False,
        # A backed account's username is owned by its provider account and
        # refreshed by propagation; writing it here is refused.
        service_provider_account__isnull=True,
    ).select_related("offering__customer__serviceprovider")

    for offering_user in offering_users:
        if (
            offering_user.offering.resolve_account_setting("username_generation_policy")
            != policy
        ):
            continue
        logger.info(
            "Updating %s username after FreeIPA profile %s change",
            offering_user,
            profile,
        )
        old_username = offering_user.username
        new_username = marketplace_utils.generate_username(
            profile.user, offering_user.offering
        )

        logger.info(
            "OfferingUser username refresh after FreeIPA profile update: offering_user_uuid=%s offering_uuid=%s old_username=%r new_username=%r affected_user_uuid=%s",
            offering_user.uuid.hex,
            offering_user.offering.uuid.hex,
            old_username,
            new_username,
            profile.user.uuid.hex,
        )
        offering_user.username = new_username
        # Call save() without update_fields to trigger state transition logic in OfferingUser.save()
        # This ensures state is updated from CREATION_REQUESTED to OK when username becomes available
        offering_user.save()
