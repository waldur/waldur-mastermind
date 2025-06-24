import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_core.core.log import event_logger
from waldur_core.core.models import SshPublicKey, User
from waldur_core.quotas.models import QuotaLimit

from . import models, tasks, utils
from .models import Profile

logger = logging.getLogger(__name__)


def schedule_sync(*args, **kwargs):
    tasks.schedule_sync()


def schedule_sync_on_quota_change(
    sender, instance: QuotaLimit, created=False, **kwargs
):
    if instance.name != utils.QUOTA_NAME:
        return
    if created and instance.value == -1:
        return
    tasks.schedule_sync()


def log_profile_event(sender, instance: Profile, created=False, **kwargs):
    profile = instance

    if created:
        event_logger.info(
            "{username} FreeIPA profile has been created.",
            event_type="freeipa_profile_created",
            event_context={
                "user": profile.user,
                "username": profile.username,
            },
            group="freeipa",
        )

    elif profile.tracker.has_changed("is_active") and profile.tracker.previous(
        "is_active"
    ):
        event_logger.info(
            "{username} FreeIPA profile has been disabled.",
            event_type="freeipa_profile_disabled",
            event_context={
                "user": profile.user,
                "username": profile.username,
            },
            group="freeipa",
        )

    elif profile.tracker.has_changed("is_active") and not profile.tracker.previous(
        "is_active"
    ):
        event_logger.info(
            "{username} FreeIPA profile has been enabled.",
            event_type="freeipa_profile_enabled",
            event_context={
                "user": profile.user,
                "username": profile.username,
            },
            group="freeipa",
        )


def log_profile_deleted(sender, instance: Profile, **kwargs):
    profile = instance
    event_logger.info(
        "{username} FreeIPA profile has been deleted.",
        event_type="freeipa_profile_deleted",
        event_context={
            "user": profile.user,
            "username": profile.username,
        },
        group="freeipa",
    )


def schedule_ssh_key_sync_when_key_is_created(
    sender, instance: SshPublicKey, created=False, **kwargs
):
    if created:
        schedule_ssh_key_sync(instance)


def schedule_ssh_key_sync_when_key_is_deleted(sender, instance: SshPublicKey, **kwargs):
    schedule_ssh_key_sync(instance)


def schedule_ssh_key_sync(ssh_key):
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
    user = instance

    if created:
        return

    if set(user.tracker.changed()) & {"is_active"}:
        try:
            profile = models.Profile.objects.get(user=user)
        except models.Profile.DoesNotExist:
            logger.warning(f"No FreeIPA profile found for user {user.username}.")
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
