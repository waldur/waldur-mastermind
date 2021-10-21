import logging

from django.conf import settings
from django.db import transaction

from waldur_core.core.utils import serialize_instance
from waldur_core.structure import models as structure_models
from waldur_core.structure import signals as structure_signals

from . import tasks

logger = logging.getLogger(__name__)


def update_remote_project_permission(sender, instance, user, **kwargs):
    if not settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
        return

    transaction.on_commit(
        lambda: tasks.update_remote_project_permissions.delay(
            serialize_instance(instance.project),
            serialize_instance(instance.user),
            instance.role,
            True,
            instance.expiration_time,
        )
    )


def sync_permission_with_remote_project(
    sender, structure, user, role, signal, expiration_time=None, **kwargs
):
    if not settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
        return

    if user.registration_method != 'eduteams':
        return

    grant = signal == structure_signals.structure_role_granted

    transaction.on_commit(
        lambda: tasks.update_remote_project_permissions.delay(
            serialize_instance(structure),
            serialize_instance(user),
            role,
            grant,
            expiration_time,
        )
    )


def sync_permission_with_remote_customer(
    sender, structure, user, role, signal, expiration_time=None, **kwargs
):
    if not settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
        return

    if user.registration_method != 'eduteams':
        return

    if role != structure_models.CustomerRole.OWNER:
        # Skip support role synchronization
        return

    grant = signal == structure_signals.structure_role_granted

    transaction.on_commit(
        lambda: tasks.update_remote_customer_permissions.delay(
            serialize_instance(structure),
            serialize_instance(user),
            role,
            grant,
            expiration_time,
        )
    )


def sync_remote_project(sender, instance, created=False, **kwargs):
    if not settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
        return

    if created:
        return
    transaction.on_commit(
        lambda: tasks.sync_remote_project.delay(serialize_instance(instance))
    )
