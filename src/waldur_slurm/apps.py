from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals


class SlurmConfig(AppConfig):
    name = 'waldur_slurm'
    verbose_name = 'SLURM'
    service_name = 'SLURM'

    def ready(self):
        from waldur_core.quotas.fields import QuotaField, CounterQuotaField
        from waldur_core.structure import SupportedServices
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals
        from waldur_freeipa import models as freeipa_models

        from .backend import SlurmBackend
        from . import handlers, models, utils

        SupportedServices.register_backend(SlurmBackend)

        signals.post_save.connect(
            handlers.process_user_creation,
            sender=freeipa_models.Profile,
            dispatch_uid='waldur_slurm.handlers.process_user_creation',
        )

        signals.pre_delete.connect(
            handlers.process_user_deletion,
            sender=freeipa_models.Profile,
            dispatch_uid='waldur_slurm.handlers.process_user_deletion',
        )

        structure_models_with_roles = (structure_models.Customer, structure_models.Project)
        for model in structure_models_with_roles:
            structure_signals.structure_role_granted.connect(
                handlers.process_role_granted,
                sender=model,
                dispatch_uid='waldur_slurm.handlers.process_role_granted.%s' % model.__class__,
            )

            structure_signals.structure_role_revoked.connect(
                handlers.process_role_revoked,
                sender=model,
                dispatch_uid='waldur_slurm.handlers.process_role_revoked.%s' % model.__class__,
            )

        for quota in utils.QUOTA_NAMES:
            structure_models.Customer.add_quota_field(
                name=quota,
                quota_field=QuotaField(is_backend=True)
            )

            structure_models.Project.add_quota_field(
                name=quota,
                quota_field=QuotaField(is_backend=True)
            )

        structure_models.Project.add_quota_field(
            name='nc_allocation_count',
            quota_field=CounterQuotaField(
                target_models=lambda: [models.Allocation],
                path_to_scope='service_project_link.project',
            )
        )

        structure_models.Customer.add_quota_field(
            name='nc_allocation_count',
            quota_field=CounterQuotaField(
                target_models=lambda: [models.Allocation],
                path_to_scope='service_project_link.project.customer',
            )
        )

        signals.post_save.connect(
            handlers.update_quotas_on_allocation_usage_update,
            sender=models.Allocation,
            dispatch_uid='waldur_slurm.handlers.update_quotas_on_allocation_usage_update',
        )
