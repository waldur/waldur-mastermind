"""Signal handlers for SCIM integration."""

from django.db import transaction

from waldur_core.configuration import config


def schedule_user_sync(sender, instance, **kwargs):
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED:
        return

    from . import tasks

    if not tasks.is_scim_configured():
        return

    transaction.on_commit(
        lambda: tasks.sync_user_entitlements.delay(instance.user.uuid.hex)
    )
