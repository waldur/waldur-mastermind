from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class FreeIPAConfig(AppConfig):
    name = 'waldur_freeipa'
    verbose_name = 'FreeIPA'

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.quotas.fields import QuotaField
        from waldur_core.quotas import models as quota_models
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals

        from . import handlers, utils, models

        for model in (structure_models.Customer, structure_models.Project):
            signals.post_save.connect(
                handlers.schedule_sync,
                sender=model,
                dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_%s_creation' % model.__class__,
            )

            signals.pre_delete.connect(
                handlers.schedule_sync,
                sender=model,
                dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_%s_deletion' % model.__class__,
            )

            structure_signals.structure_role_granted.connect(
                handlers.schedule_sync,
                sender=model,
                dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_%s_role_granted' % model.__class__,
            )

            structure_signals.structure_role_revoked.connect(
                handlers.schedule_sync,
                sender=model,
                dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_%s_role_revoked' % model.__class__,
            )

        signals.post_save.connect(
            handlers.schedule_sync_on_quota_change,
            sender=quota_models.Quota,
            dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_quota_save',
        )

        signals.post_save.connect(
            handlers.schedule_ssh_key_sync_when_key_is_created,
            sender=core_models.SshPublicKey,
            dispatch_uid='waldur_freeipa.handlers.schedule_ssh_key_sync_when_key_is_created',
        )

        signals.pre_delete.connect(
            handlers.schedule_ssh_key_sync_when_key_is_deleted,
            sender=core_models.SshPublicKey,
            dispatch_uid='waldur_freeipa.handlers.schedule_ssh_key_sync_when_key_is_deleted',
        )

        structure_models.Customer.add_quota_field(
            name=utils.QUOTA_NAME,
            quota_field=QuotaField()
        )

        structure_models.Project.add_quota_field(
            name=utils.QUOTA_NAME,
            quota_field=QuotaField()
        )

        signals.pre_save.connect(
            handlers.log_profile_event,
            sender=models.Profile,
            dispatch_uid='waldur_freeipa.handlers.log_profile_event',
        )

        signals.pre_delete.connect(
            handlers.log_profile_deleted,
            sender=models.Profile,
            dispatch_uid='waldur_freeipa.handlers.log_profile_deleted',
        )
