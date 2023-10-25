from django.apps import AppConfig
from django.db.models import signals


class FreeIPAConfig(AppConfig):
    name = 'waldur_freeipa'
    verbose_name = 'FreeIPA'

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas import models as quota_models
        from waldur_core.quotas.fields import QuotaField
        from waldur_core.structure import models as structure_models
        from waldur_slurm import models as slurm_models
        from waldur_slurm import signals as slurm_signals

        from . import handlers, models, utils

        for model in (structure_models.Customer, structure_models.Project):
            signals.post_save.connect(
                handlers.schedule_sync,
                sender=model,
                dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_%s_creation'
                % model.__class__,
            )

            signals.pre_delete.connect(
                handlers.schedule_sync,
                sender=model,
                dispatch_uid='waldur_freeipa.handlers.schedule_sync_on_%s_deletion'
                % model.__class__,
            )

        permission_signals.role_granted.connect(
            handlers.schedule_sync,
            dispatch_uid='waldur_freeipa.handlers.schedule_sync_when_role_granted',
        )

        permission_signals.role_revoked.connect(
            handlers.schedule_sync,
            dispatch_uid='waldur_freeipa.handlers.schedule_sync_when_role_revoked',
        )

        signals.post_save.connect(
            handlers.schedule_sync_on_quota_change,
            sender=quota_models.QuotaLimit,
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
            name=utils.QUOTA_NAME, quota_field=QuotaField()
        )

        structure_models.Project.add_quota_field(
            name=utils.QUOTA_NAME, quota_field=QuotaField()
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

        slurm_signals.slurm_association_created.connect(
            handlers.enable_profile_when_association_is_created,
            sender=slurm_models.Allocation,
            dispatch_uid='waldur_mastermind.marketplace_slurm.enable_profile_when_association_is_created',
        )

        signals.post_save.connect(
            handlers.update_user,
            sender=core_models.User,
            dispatch_uid='waldur_freeipa.handlers.update_user',
        )
