from django.apps import AppConfig
from django.db.models import signals
from django_fsm import signals as fsm_signals


class StructureConfig(AppConfig):
    name = "waldur_core.structure"
    verbose_name = "Structure"

    def ready(self):
        from django.core import checks

        from waldur_core.core.models import ChangeEmailRequest, User
        from waldur_core.permissions import signals as permission_signals
        from waldur_core.quotas import signals as quota_signals
        from waldur_core.structure import handlers
        from waldur_core.structure import signals as structure_signals
        from waldur_core.structure.executors import check_cleanup_executors
        from waldur_core.structure.models import (
            BaseResource,
            SubResource,
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

        resource_and_subresources = set(
            BaseResource.get_all_models() + SubResource.get_all_models()
        )
        for index, model in enumerate(resource_and_subresources):
            signals.pre_delete.connect(
                handlers.log_resource_deleted,
                sender=model,
                dispatch_uid="waldur_core.structure.handlers.log_resource_deleted_{}_{}".format(
                    model.__name__, index
                ),
            )

            structure_signals.resource_imported.connect(
                handlers.log_resource_imported,
                sender=model,
                dispatch_uid="waldur_core.structure.handlers.log_resource_imported_{}_{}".format(
                    model.__name__, index
                ),
            )

            fsm_signals.post_transition.connect(
                handlers.log_resource_action,
                sender=model,
                dispatch_uid="waldur_core.structure.handlers.log_resource_action_{}_{}".format(
                    model.__name__, index
                ),
            )

            signals.post_save.connect(
                handlers.log_resource_creation_scheduled,
                sender=model,
                dispatch_uid="waldur_core.structure.handlers.log_resource_creation_scheduled_{}_{}".format(
                    model.__name__, index
                ),
            )

            signals.pre_delete.connect(
                handlers.delete_service_settings_on_scope_delete,
                sender=model,
                dispatch_uid="waldur_core.structure.handlers.delete_service_settings_on_scope_delete_{}_{}".format(
                    model.__name__, index
                ),
            )

        for index, model in enumerate(VirtualMachine.get_all_models()):
            signals.post_save.connect(
                handlers.update_resource_start_time,
                sender=model,
                dispatch_uid="waldur_core.structure.handlers.update_resource_start_time_{}_{}".format(
                    model.__name__, index
                ),
            )

        signals.post_save.connect(
            handlers.notify_about_user_profile_changes,
            sender=User,
            dispatch_uid="waldur_core.structure.handlers.notify_about_user_profile_changes",
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
