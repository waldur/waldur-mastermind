from django.apps import AppConfig
from django.db.models import signals


class OpenPortalConfig(AppConfig):
    name = "waldur_openportal"
    verbose_name = "OpenPortal"
    service_name = "OpenPortal"

    def ready(self):
        # These need to be imported here to avoid circular imports
        # This is the same as in waldur_slurm.apps.py
        from waldur_core.core import models as core_models
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas.fields import CounterQuotaField, QuotaField
        from waldur_core.structure import models as structure_models
        from waldur_core.structure.registry import SupportedServices
        from waldur_mastermind.marketplace import models as marketplace_models

        from . import handlers, models, utils
        from .backend import OpenPortalBackend

        SupportedServices.register_backend(OpenPortalBackend)

        signals.post_save.connect(
            handlers.update_user,
            sender=core_models.User,
            dispatch_uid="waldur_openportal.handlers.update_user",
        )

        signals.pre_delete.connect(
            handlers.delete_user,
            sender=core_models.User,
            dispatch_uid="waldur_openportal.handlers.delete_user",
        )

        signals.post_save.connect(
            handlers.update_project,
            sender=structure_models.Project,
            dispatch_uid="waldur_openportal.handlers.update_project",
        )

        signals.pre_delete.connect(
            handlers.delete_project,
            sender=structure_models.Project,
            dispatch_uid="waldur_openportal.handlers.delete_project",
        )

        signals.post_save.connect(
            handlers.update_allocation_credits,
            sender=marketplace_models.Resource,
            dispatch_uid="waldur_openportal.handlers.update_allocation_credits",
        )

        signals.post_save.connect(
            handlers.update_offering_agents,
            sender=models.ProjectTemplate,
            dispatch_uid="waldur_openportal.handlers.update_offering_agents",
        )

        permission_signals.role_granted.connect(
            handlers.role_granted,
            dispatch_uid="waldur_openportal.handlers.role_granted",
        )

        permission_signals.role_revoked.connect(
            handlers.role_revoked,
            dispatch_uid="waldur_openportal.handlers.role_revoked",
        )

        for quota in utils.QUOTA_NAMES:
            structure_models.Customer.add_quota_field(
                name=quota, quota_field=QuotaField(is_backend=True)
            )

            structure_models.Project.add_quota_field(
                name=quota, quota_field=QuotaField(is_backend=True)
            )

        structure_models.Project.add_quota_field(
            name="op_allocation_count",
            quota_field=CounterQuotaField(
                target_models=lambda: [models.Allocation],
                path_to_scope="project",
            ),
        )

        structure_models.Project.add_quota_field(
            name="op_remote_allocation_count",
            quota_field=CounterQuotaField(
                target_models=lambda: [models.RemoteAllocation],
                path_to_scope="project",
            ),
        )

        structure_models.Customer.add_quota_field(
            name="op_allocation_count",
            quota_field=CounterQuotaField(
                target_models=lambda: [models.Allocation],
                path_to_scope="project.customer",
            ),
        )

        structure_models.Customer.add_quota_field(
            name="op_remote_allocation_count",
            quota_field=CounterQuotaField(
                target_models=lambda: [models.RemoteAllocation],
                path_to_scope="project.customer",
            ),
        )

        signals.post_save.connect(
            handlers.update_quotas_on_allocation_usage_update,
            sender=models.Allocation,
            dispatch_uid="waldur_openportal.handlers.update_quotas_on_allocation_usage_update",
        )

        signals.post_save.connect(
            handlers.update_quotas_on_remote_allocation_usage_update,
            sender=models.RemoteAllocation,
            dispatch_uid=(
                "waldur_openportal.handlers."
                "update_quotas_on_remote_allocation_usage_update"
            ),
        )

        signals.post_save.connect(
            handlers.sync_project_credits_on_remote_allocation_change,
            sender=models.RemoteAllocation,
            dispatch_uid=(
                "waldur_openportal.handlers."
                "sync_project_credits_on_remote_allocation_change"
            ),
        )
