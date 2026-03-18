import importlib
import logging

from django.apps import AppConfig
from django.db.models import signals
from django_fsm import signals as fsm_signals

logger = logging.getLogger(__name__)


class StructureConfig(AppConfig):
    name = "waldur_core.structure"
    verbose_name = "Structure"

    def ready(self):
        from django.core import checks

        self._register_digest_providers()

        from waldur_core.core.models import ChangeEmailRequest
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas import signals as quota_signals
        from waldur_core.structure import handlers
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.executors import check_cleanup_executors
        from waldur_core.structure.models import (
            AccessSubnet,
            BaseResource,
            ProjectEndDateChangeRequest,
            VirtualMachine,
        )
        from waldur_core.users.models import PermissionRequest

        checks.register(check_cleanup_executors)

        Customer = self.get_model("Customer")
        Project = self.get_model("Project")

        permission_signals.role_granted.connect(
            handlers.change_users_quota,
            dispatch_uid="waldur_core.structure.increase_users_quota_when_role_is_granted",
        )

        permission_signals.role_revoked.connect(
            handlers.change_users_quota,
            dispatch_uid="waldur_core.structure.increase_users_quota_when_role_is_granted",
        )

        signals.post_save.connect(
            handlers.log_customer_save,
            sender=Customer,
            dispatch_uid="waldur_core.structure.handlers.log_customer_save",
        )

        signals.post_delete.connect(
            handlers.log_customer_delete,
            sender=Customer,
            dispatch_uid="waldur_core.structure.handlers.log_customer_delete",
        )

        from waldur_core.core.handlers import create_initial_revision

        signals.post_save.connect(
            create_initial_revision,
            sender=Customer,
            dispatch_uid="waldur_core.structure.create_initial_revision_Customer",
        )

        signals.post_save.connect(
            handlers.log_project_save,
            sender=Project,
            dispatch_uid="waldur_core.structure.handlers.log_project_save",
        )

        signals.post_delete.connect(
            handlers.log_project_delete,
            sender=Project,
            dispatch_uid="waldur_core.structure.handlers.log_project_delete",
        )

        signals.pre_delete.connect(
            handlers.revoke_roles_on_project_deletion,
            sender=Project,
            dispatch_uid="waldur_core.structure.handlers.revoke_roles_on_project_deletion",
        )

        for index, model in enumerate(BaseResource.get_all_models()):
            signals.pre_delete.connect(
                handlers.log_resource_deleted,
                sender=model,
                dispatch_uid=f"waldur_core.structure.handlers.log_resource_deleted_{model.__name__}_{index}",
            )

            structure_signals.resource_imported.connect(
                handlers.log_resource_imported,
                sender=model,
                dispatch_uid=f"waldur_core.structure.handlers.log_resource_imported_{model.__name__}_{index}",
            )

            fsm_signals.post_transition.connect(
                handlers.log_resource_action,
                sender=model,
                dispatch_uid=f"waldur_core.structure.handlers.log_resource_action_{model.__name__}_{index}",
            )

            signals.post_save.connect(
                handlers.log_resource_creation_scheduled,
                sender=model,
                dispatch_uid=f"waldur_core.structure.handlers.log_resource_creation_scheduled_{model.__name__}_{index}",
            )

            signals.pre_delete.connect(
                handlers.delete_service_settings_on_scope_delete,
                sender=model,
                dispatch_uid=f"waldur_core.structure.handlers.delete_service_settings_on_scope_delete_{model.__name__}_{index}",
            )

        for index, model in enumerate(VirtualMachine.get_all_models()):
            signals.post_save.connect(
                handlers.update_resource_start_time,
                sender=model,
                dispatch_uid=f"waldur_core.structure.handlers.update_resource_start_time_{model.__name__}_{index}",
            )

        permission_signals.role_granted.connect(
            handlers.change_users_quota,
            dispatch_uid="waldur_core.structure.increase_users_quota_when_role_is_granted",
        )

        quota_signals.recalculate_quotas.connect(
            handlers.update_customer_users_count,
            dispatch_uid="waldur_core.structure.handlers.update_customer_users_count",
        )

        signals.post_save.connect(
            handlers.change_email_has_been_requested,
            sender=ChangeEmailRequest,
            dispatch_uid="waldur_core.structure.handlers.change_email_has_been_requested",
        )

        structure_signals.permissions_request_approved.connect(
            handlers.permissions_request_approved,
            sender=PermissionRequest,
            dispatch_uid="waldur_core.structure.handlers.permissions_request_approved",
        )

        signals.post_save.connect(
            handlers.log_access_subnet_save,
            sender=AccessSubnet,
            dispatch_uid="waldur_core.structure.handlers.log_access_subnet_creation_succeeded",
        )

        signals.post_delete.connect(
            handlers.log_access_subnet_deletion_succeeded,
            sender=AccessSubnet,
            dispatch_uid="waldur_core.structure.handlers.log_access_subnet_deletion_succeeded",
        )

        # Project metadata signal handlers
        signals.post_save.connect(
            handlers.create_project_metadata_completion,
            sender=Project,
            dispatch_uid="waldur_core.structure.create_project_metadata_completion",
        )

        signals.post_save.connect(
            handlers.create_existing_projects_completions,
            sender=Customer,
            dispatch_uid="waldur_core.structure.create_existing_projects_completions",
        )

        signals.post_save.connect(
            handlers.delete_project_metadata_completions,
            sender=Customer,
            dispatch_uid="waldur_core.structure.delete_project_metadata_completions",
        )

        signals.post_save.connect(
            handlers.log_project_end_date_change_request_events,
            sender=ProjectEndDateChangeRequest,
            dispatch_uid="waldur_core.structure.log_project_end_date_change_request_events",
        )

    def _register_digest_providers(self):
        """Auto-discover and register project digest providers from all apps."""
        from django.apps import apps

        for app_config in apps.get_app_configs():
            try:
                module_name = f"{app_config.name}.project_digest"
                importlib.import_module(module_name)
            except ImportError:
                continue
            except Exception as e:
                logger.warning(
                    "Error loading project digest providers from %s: %s",
                    app_config.name,
                    e,
                )
