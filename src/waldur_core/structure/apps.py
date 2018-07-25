from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals
from django_fsm import signals as fsm_signals


class StructureConfig(AppConfig):
    name = 'waldur_core.structure'
    verbose_name = 'Structure'

    def ready(self):
        from waldur_core.core.models import CoordinatesMixin, User
        from waldur_core.structure.executors import check_cleanup_executors
        from waldur_core.structure.models import ResourceMixin, Service, TagMixin, VirtualMachine
        from waldur_core.structure import handlers
        from waldur_core.structure import signals as structure_signals

        from django.core import checks
        checks.register(check_cleanup_executors)

        Customer = self.get_model('Customer')
        Project = self.get_model('Project')

        CustomerPermission = self.get_model('CustomerPermission')
        ProjectPermission = self.get_model('ProjectPermission')

        signals.post_save.connect(
            handlers.log_customer_save,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.log_customer_save',
        )

        signals.post_delete.connect(
            handlers.log_customer_delete,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.log_customer_delete',
        )

        signals.post_save.connect(
            handlers.log_project_save,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.log_project_save',
        )

        signals.post_delete.connect(
            handlers.log_project_delete,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.log_project_delete',
        )

        # increase nc_user_count quota usage on adding user to customer
        structure_models_with_roles = (Customer, Project)
        for model in structure_models_with_roles:
            name = 'increase_customer_nc_users_quota_on_adding_user_to_%s' % model.__name__
            structure_signals.structure_role_granted.connect(
                handlers.change_customer_nc_users_quota,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.%s' % name,
            )

        # decrease nc_user_count quota usage on removing user from customer
        for model in structure_models_with_roles:
            name = 'decrease_customer_nc_users_quota_on_removing_user_from_%s' % model.__name__
            structure_signals.structure_role_revoked.connect(
                handlers.change_customer_nc_users_quota,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.%s' % name,
            )

        structure_signals.structure_role_granted.connect(
            handlers.log_customer_role_granted,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.log_customer_role_granted',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.log_customer_role_revoked,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.log_customer_role_revoked',
        )

        structure_signals.structure_role_updated.connect(
            handlers.log_customer_role_updated,
            sender=CustomerPermission,
            dispatch_uid='waldur_core.structure.handlers.log_customer_role_updated',
        )

        structure_signals.structure_role_granted.connect(
            handlers.log_project_role_granted,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.log_project_role_granted',
        )

        structure_signals.structure_role_revoked.connect(
            handlers.log_project_role_revoked,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.log_project_role_revoked',
        )

        structure_signals.structure_role_updated.connect(
            handlers.log_project_role_updated,
            sender=ProjectPermission,
            dispatch_uid='waldur_core.structure.handlers.log_project_role_updated',
        )

        signals.pre_delete.connect(
            handlers.revoke_roles_on_project_deletion,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.revoke_roles_on_project_deletion',
        )

        for index, model in enumerate(ResourceMixin.get_all_models()):
            signals.pre_delete.connect(
                handlers.log_resource_deleted,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.log_resource_deleted_{}_{}'.format(
                    model.__name__, index),
            )

            structure_signals.resource_imported.connect(
                handlers.log_resource_imported,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.log_resource_imported_{}_{}'.format(
                    model.__name__, index),
            )

            fsm_signals.post_transition.connect(
                handlers.log_resource_action,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.log_resource_action_{}_{}'.format(
                    model.__name__, index),
            )

            signals.post_save.connect(
                handlers.log_resource_creation_scheduled,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.log_resource_creation_scheduled_{}_{}'.format(
                    model.__name__, index),
            )

            signals.pre_delete.connect(
                handlers.delete_service_settings_on_scope_delete,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.delete_service_settings_on_scope_delete_{}_{}'.format(
                    model.__name__, index),
            )

            if issubclass(model, CoordinatesMixin):
                fsm_signals.post_transition.connect(
                    handlers.detect_vm_coordinates,
                    sender=model,
                    dispatch_uid='waldur_core.structure.handlers.detect_vm_coordinates_{}_{}'.format(
                        model.__name__, index),
                )

        for index, model in enumerate(VirtualMachine.get_all_models()):
            signals.post_save.connect(
                handlers.update_resource_start_time,
                sender=model,
                dispatch_uid='waldur_core.structure.handlers.update_resource_start_time_{}_{}'.format(
                    model.__name__, index),
            )

        signals.post_save.connect(
            handlers.connect_customer_to_shared_service_settings,
            sender=Customer,
            dispatch_uid='waldur_core.structure.handlers.connect_customer_to_shared_service_settings',
        )

        signals.post_save.connect(
            handlers.connect_project_to_all_available_services,
            sender=Project,
            dispatch_uid='waldur_core.structure.handlers.connect_project_to_all_available_services',
        )

        for index, service_model in enumerate(Service.get_all_models()):
            signals.post_save.connect(
                handlers.connect_service_to_all_projects_if_it_is_available_for_all,
                sender=service_model,
                dispatch_uid='waldur_core.structure.handlers.'
                             'connect_service_{}_to_all_projects_if_it_is_available_for_all_{}'.format(
                                 service_model.__name__, index),
            )

            signals.post_delete.connect(
                handlers.delete_service_settings_on_service_delete,
                sender=service_model,
                dispatch_uid='waldur_core.structure.handlers.delete_service_settings_on_service_delete_{}_{}'.format(
                    service_model.__name__, index),
            )

        signals.post_save.connect(
            handlers.clean_tags_cache_after_tagged_item_saved,
            sender=TagMixin.tags.through,
            dispatch_uid='waldur_core.structure.handlers.clean_tags_cache_after_tagged_item_created'
        )

        signals.pre_delete.connect(
            handlers.clean_tags_cache_before_tagged_item_deleted,
            sender=TagMixin.tags.through,
            dispatch_uid='waldur_core.structure.handlers.clean_tags_cache_after_tagged_item_created'
        )

        signals.post_save.connect(
            handlers.notify_about_user_profile_changes,
            sender=User,
            dispatch_uid='waldur_core.structure.handlers.notify_about_user_profile_changes',
        )
