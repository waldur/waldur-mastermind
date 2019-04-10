from django.apps import AppConfig
from django.db.models import signals
from django_fsm import signals as fsm_signals


class OpenStackTenantConfig(AppConfig):
    """ OpenStack is a toolkit for building private and public clouds.
        This application adds support for managing OpenStack tenant resources -
        instances, volumes and snapshots.
    """
    name = 'waldur_openstack.openstack_tenant'
    label = 'openstack_tenant'
    verbose_name = 'OpenStackTenant'
    service_name = 'OpenStackTenant'

    def ready(self):
        from waldur_core.quotas.fields import QuotaField, TotalQuotaField
        from waldur_core.structure.models import ServiceSettings, Project, Customer
        from waldur_core.structure import SupportedServices
        from waldur_openstack.openstack.models import Tenant

        from .backend import OpenStackTenantBackend
        from . import handlers, models

        SupportedServices.register_backend(OpenStackTenantBackend)

        # Initialize service settings quotas based on tenant.
        for quota in Tenant.get_quotas_fields():
            ServiceSettings.add_quota_field(
                name=quota.name,
                quota_field=QuotaField(
                    is_backend=True,
                    default_limit=quota.default_limit,
                    creation_condition=lambda service_settings:
                        service_settings.type == OpenStackTenantConfig.service_name
                )
            )

        Project.add_quota_field(
            name='os_cpu_count',
            quota_field=TotalQuotaField(
                target_models=[models.Instance],
                path_to_scope='service_project_link.project',
                target_field='cores',
            )
        )

        Project.add_quota_field(
            name='os_ram_size',
            quota_field=TotalQuotaField(
                target_models=[models.Instance],
                path_to_scope='service_project_link.project',
                target_field='ram',
            )
        )

        Project.add_quota_field(
            name='os_storage_size',
            quota_field=TotalQuotaField(
                target_models=[models.Volume, models.Snapshot],
                path_to_scope='service_project_link.project',
                target_field='size',
            )
        )

        Customer.add_quota_field(
            name='os_cpu_count',
            quota_field=TotalQuotaField(
                target_models=[models.Instance],
                path_to_scope='service_project_link.project.customer',
                target_field='cores',
            )
        )

        Customer.add_quota_field(
            name='os_ram_size',
            quota_field=TotalQuotaField(
                target_models=[models.Instance],
                path_to_scope='service_project_link.project.customer',
                target_field='ram',
            )
        )

        Customer.add_quota_field(
            name='os_storage_size',
            quota_field=TotalQuotaField(
                target_models=[models.Volume, models.Snapshot],
                path_to_scope='service_project_link.project.customer',
                target_field='size',
            )
        )

        for Resource in (models.Instance, models.Volume, models.Snapshot):
            name = Resource.__name__.lower()
            signals.post_save.connect(
                handlers.log_action,
                sender=Resource,
                dispatch_uid='openstack_tenant.handlers.log_%s_action' % name,
            )

        for handler in handlers.resource_handlers:
            model = handler.resource_model
            name = model.__name__.lower()

            fsm_signals.post_transition.connect(
                handler.create_handler,
                sender=model,
                dispatch_uid='openstack_tenant.handlers.create_%s' % name,
            )

            fsm_signals.post_transition.connect(
                handler.update_handler,
                sender=model,
                dispatch_uid='openstack_tenant.handlers.update_%s' % name,
            )

            signals.post_save.connect(
                handler.import_handler,
                sender=model,
                dispatch_uid='openstack_tenant.handlers.import_%s' % name,
            )

            signals.post_delete.connect(
                handler.delete_handler,
                sender=model,
                dispatch_uid='openstack_tenant.handlers.delete_%s' % name,
            )

        signals.post_save.connect(
            handlers.log_backup_schedule_creation,
            sender=models.BackupSchedule,
            dispatch_uid='openstack_tenant.handlers.log_backup_schedule_creation',
        )

        signals.post_save.connect(
            handlers.log_backup_schedule_action,
            sender=models.BackupSchedule,
            dispatch_uid='openstack_tenant.handlers.log_backup_schedule_action',
        )

        signals.pre_delete.connect(
            handlers.log_backup_schedule_deletion,
            sender=models.BackupSchedule,
            dispatch_uid='openstack_tenant.handlers.log_backup_schedule_deletion',
        )

        signals.post_save.connect(
            handlers.log_snapshot_schedule_creation,
            sender=models.SnapshotSchedule,
            dispatch_uid='openstack_tenant.handlers.log_snapshot_schedule_creation',
        )

        signals.post_save.connect(
            handlers.log_snapshot_schedule_action,
            sender=models.SnapshotSchedule,
            dispatch_uid='openstack_tenant.handlers.log_snapshot_schedule_action',
        )

        signals.pre_delete.connect(
            handlers.log_snapshot_schedule_deletion,
            sender=models.SnapshotSchedule,
            dispatch_uid='openstack_tenant.handlers.log_snapshot_schedule_deletion',
        )

        signals.post_save.connect(
            handlers.update_service_settings_credentials,
            sender=Tenant,
            dispatch_uid='openstack_tenant.handlers.update_service_settings_credentials',
        )

        signals.post_save.connect(
            handlers.update_service_settings,
            sender=Tenant,
            dispatch_uid='openstack_tenant.handlers.update_service_settings',
        )

        signals.m2m_changed.connect(
            handlers.sync_certificates_between_openstack_service_with_openstacktenant_service,
            sender=ServiceSettings.certifications.through,
            dispatch_uid='openstack_tenant.handlers.'
                         'sync_certificates_between_openstack_service_with_openstacktenant_service',
        )

        signals.post_save.connect(
            handlers.copy_certifications_from_openstack_service_to_openstacktenant_service,
            sender=ServiceSettings,
            dispatch_uid='openstack_tenant.handlers.'
                         'copy_certifications_from_openstack_service_to_openstacktenant_service',
        )

        signals.post_save.connect(
            handlers.copy_flavor_exclude_regex_to_openstacktenant_service_settings,
            sender=ServiceSettings,
            dispatch_uid='openstack_tenant.handlers.'
                         'copy_flavor_exclude_regex_to_openstacktenant_service_settings',
        )

        signals.post_save.connect(
            handlers.create_service_from_tenant,
            sender=Tenant,
            dispatch_uid='openstack_tenant.handlers.create_service_from_tenant',
        )

        signals.post_save.connect(
            handlers.sync_price_list_item_for_flavor,
            sender=models.Flavor,
            dispatch_uid='openstack_tenant.handlers.sync_price_list_item_for_flavor',
        )
