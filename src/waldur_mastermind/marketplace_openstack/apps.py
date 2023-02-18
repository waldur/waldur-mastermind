from django.apps import AppConfig
from django.conf import settings
from django.db.models import signals


def get_secret_attributes():
    if not settings.WALDUR_OPENSTACK['TENANT_CREDENTIALS_VISIBLE']:
        return 'user_username', 'user_password'


def components_filter(offering, qs):
    from . import AVAILABLE_LIMITS, STORAGE_MODE_FIXED, STORAGE_TYPE

    storage_mode = offering.plugin_options.get('storage_mode') or STORAGE_MODE_FIXED
    if storage_mode == STORAGE_MODE_FIXED:
        qs = qs.filter(type__in=AVAILABLE_LIMITS)
    else:
        qs = qs.exclude(type=STORAGE_TYPE)
    return qs


class MarketplaceOpenStackConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_openstack'
    verbose_name = 'Marketplace OpenStack'

    def ready(self):
        from waldur_core.quotas import models as quota_models
        from waldur_core.structure import models as structure_models
        from waldur_core.structure import signals as structure_signals
        from waldur_mastermind.marketplace import filters as marketplace_filters
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import Component, manager
        from waldur_mastermind.marketplace_openstack.registrators import (
            OpenStackInstanceRegistrator,
            OpenStackTenantRegistrator,
        )
        from waldur_openstack.openstack import models as openstack_models
        from waldur_openstack.openstack import signals as openstack_signals
        from waldur_openstack.openstack.apps import OpenStackConfig
        from waldur_openstack.openstack_tenant import executors as tenant_executors
        from waldur_openstack.openstack_tenant import models as tenant_models
        from waldur_openstack.openstack_tenant.apps import OpenStackTenantConfig

        from . import (
            AVAILABLE_LIMITS,
            CORES_TYPE,
            INSTANCE_TYPE,
            RAM_TYPE,
            SHARED_INSTANCE_TYPE,
            STORAGE_TYPE,
            TENANT_TYPE,
            VOLUME_TYPE,
            filters,
            handlers,
            processors,
        )

        marketplace_filters.ExternalOfferingFilterBackend.register(
            filters.VpcExternalFilter()
        )

        signals.post_save.connect(
            handlers.create_offering_from_tenant,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketplace_openstack.create_offering_from_tenant',
        )

        signals.pre_delete.connect(
            handlers.archive_offering,
            sender=structure_models.ServiceSettings,
            dispatch_uid='waldur_mastermind.marketplace_openstack.archive_offering',
        )

        resource_models = (
            tenant_models.Instance,
            tenant_models.Volume,
            openstack_models.Tenant,
        )
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)
        marketplace_handlers.connect_resource_handlers(*resource_models)

        LIMIT = marketplace_models.OfferingComponent.BillingTypes.LIMIT
        USAGE = marketplace_models.OfferingComponent.BillingTypes.USAGE
        manager.register(
            offering_type=TENANT_TYPE,
            create_resource_processor=processors.TenantCreateProcessor,
            update_resource_processor=processors.TenantUpdateProcessor,
            delete_resource_processor=processors.TenantDeleteProcessor,
            components=(
                Component(
                    type=CORES_TYPE,
                    name='Cores',
                    measured_unit='cores',
                    billing_type=LIMIT,
                ),
                # Price is stored per GiB but size is stored per MiB
                # therefore we need to divide size by factor when price estimate is calculated.
                Component(
                    type=RAM_TYPE,
                    name='RAM',
                    measured_unit='GB',
                    billing_type=LIMIT,
                    factor=1024,
                ),
                Component(
                    type=STORAGE_TYPE,
                    name='Storage',
                    measured_unit='GB',
                    billing_type=LIMIT,
                    factor=1024,
                ),
            ),
            service_type=OpenStackConfig.service_name,
            secret_attributes=get_secret_attributes,
            components_filter=components_filter,
            available_limits=AVAILABLE_LIMITS,
            can_update_limits=True,
            get_importable_resources_backend_method='get_importable_tenants',
            import_resource_backend_method='import_tenant',
        )

        manager.register(
            offering_type=INSTANCE_TYPE,
            create_resource_processor=processors.InstanceCreateProcessor,
            delete_resource_processor=processors.InstanceDeleteProcessor,
            get_importable_resources_backend_method='get_importable_instances',
            import_resource_backend_method='import_instance',
            import_resource_executor=tenant_executors.InstancePullExecutor,
        )

        manager.register(
            offering_type=VOLUME_TYPE,
            create_resource_processor=processors.VolumeCreateProcessor,
            delete_resource_processor=processors.VolumeDeleteProcessor,
            get_importable_resources_backend_method='get_importable_volumes',
            import_resource_backend_method='import_volume',
        )

        manager.register(
            offering_type=SHARED_INSTANCE_TYPE,
            create_resource_processor=processors.InstanceCreateProcessor,
            delete_resource_processor=processors.InstanceDeleteProcessor,
            components=(
                Component(
                    type=CORES_TYPE,
                    name='Cores',
                    measured_unit='cores',
                    billing_type=USAGE,
                ),
                Component(
                    type=RAM_TYPE,
                    name='RAM',
                    measured_unit='GB',
                    billing_type=USAGE,
                    factor=1024,
                ),
                Component(
                    type=STORAGE_TYPE,
                    name='Storage',
                    measured_unit='GB',
                    billing_type=USAGE,
                    factor=1024,
                ),
            ),
            service_type=OpenStackTenantConfig.service_name,
            secret_attributes=get_secret_attributes,
            components_filter=components_filter,
        )

        signals.post_save.connect(
            handlers.synchronize_volume_metadata,
            sender=tenant_models.Volume,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_volume_metadata',
        )

        signals.post_save.connect(
            handlers.synchronize_instance_hypervisor_hostname,
            sender=tenant_models.Instance,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_instance_hypervisor_hostname',
        )

        signals.post_save.connect(
            handlers.synchronize_tenant_name,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_tenant_name',
        )

        signals.post_save.connect(
            handlers.synchronize_instance_name,
            sender=tenant_models.Instance,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_instance_name',
        )

        signals.post_save.connect(
            handlers.synchronize_instance_after_pull,
            sender=tenant_models.Instance,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_instance_after_pull',
        )

        signals.post_save.connect(
            handlers.synchronize_internal_ips,
            sender=tenant_models.InternalIP,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_internal_ips',
        )

        signals.post_save.connect(
            handlers.synchronize_floating_ips,
            sender=tenant_models.FloatingIP,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_floating_ips',
        )

        signals.post_delete.connect(
            handlers.synchronize_internal_ips_on_delete,
            sender=tenant_models.InternalIP,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_internal_ips_on_delete',
        )

        signals.post_delete.connect(
            handlers.synchronize_floating_ips_on_delete,
            sender=tenant_models.FloatingIP,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_floating_ips_on_delete',
        )

        signals.post_save.connect(
            handlers.synchronize_directly_connected_ips,
            sender=tenant_models.Instance,
            dispatch_uid='waldur_mastermind.marketplace_openstack.synchronize_synchronize_directly_connected_ips',
        )

        signals.post_save.connect(
            handlers.create_resource_of_volume_if_instance_created,
            sender=marketplace_models.Resource,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'create_resource_of_volume_if_instance_created',
        )

        for model in [
            tenant_models.Instance,
            tenant_models.Volume,
            openstack_models.Tenant,
        ]:
            structure_signals.resource_imported.connect(
                handlers.create_marketplace_resource_for_imported_resources,
                sender=model,
                dispatch_uid='waldur_mastermind.marketplace_openstack.'
                'create_resource_for_imported_%s' % model,
            )

        signals.post_save.connect(
            handlers.import_resource_metadata_when_resource_is_created,
            sender=marketplace_models.Resource,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'import_resource_metadata_when_resource_is_created',
        )

        signals.post_save.connect(
            handlers.update_openstack_tenant_usages,
            sender=quota_models.Quota,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'update_openstack_tenant_usages',
        )

        OpenStackTenantRegistrator.connect()
        OpenStackInstanceRegistrator.connect()

        signals.post_save.connect(
            handlers.create_offering_component_for_volume_type,
            sender=openstack_models.VolumeType,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'create_offering_component_for_volume_type',
        )

        signals.post_delete.connect(
            handlers.delete_offering_component_for_volume_type,
            sender=openstack_models.VolumeType,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'delete_offering_component_for_volume_type',
        )

        signals.post_save.connect(
            handlers.synchronize_limits_when_storage_mode_is_switched,
            sender=marketplace_models.Offering,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'synchronize_limits_when_storage_mode_is_switched',
        )

        structure_signals.resource_imported.connect(
            handlers.import_instances_and_volumes_if_tenant_has_been_imported,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'import_instances_and_volumes_if_tenant_has_been_imported',
        )

        openstack_signals.tenant_pull_succeeded.connect(
            handlers.import_instances_and_volumes_if_tenant_has_been_imported,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'import_instances_and_volumes_if_tenant_has_been_imported_if_tenant_has_been_pulled',
        )

        signals.post_save.connect(
            handlers.update_usage_when_instance_configuration_is_updated,
            sender=marketplace_models.Resource,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'update_usage_when_instance_configuration_is_updated',
        )

        signals.post_save.connect(
            handlers.synchronize_router_backend_metadata,
            sender=openstack_models.Router,
            dispatch_uid='waldur_mastermind.marketplace_openstack.'
            'synchronize_router_backend_metadata',
        )
