from django.apps import AppConfig
from django.conf import settings
from django.db.models import signals


def get_secret_attributes():
    if not settings.WALDUR_OPENSTACK['TENANT_CREDENTIALS_VISIBLE']:
        return 'user_username', 'user_password'


class MarketplaceOpenStackConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_openstack'
    verbose_name = 'Marketplace OpenStack'

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_openstack.openstack import models as openstack_models
        from waldur_openstack.openstack.apps import OpenStackConfig
        from waldur_openstack.openstack_tenant import models as tenant_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace.plugins import Component

        from . import (
            handlers, processors,
            INSTANCE_TYPE, VOLUME_TYPE, PACKAGE_TYPE,
            RAM_TYPE, CORES_TYPE, STORAGE_TYPE,
        )

        signals.post_save.connect(
            handlers.create_offering_from_tenant,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketpace_openstack.create_offering_from_tenant',
        )

        signals.pre_delete.connect(
            handlers.archive_offering,
            sender=structure_models.ServiceSettings,
            dispatch_uid='waldur_mastermind.marketpace_openstack.archive_offering',
        )

        resource_models = (tenant_models.Instance, tenant_models.Volume, openstack_models.Tenant)
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)
        marketplace_handlers.connect_resource_handlers(*resource_models)

        signals.post_save.connect(
            handlers.create_template_for_plan,
            sender=marketplace_models.Plan,
            dispatch_uid='waldur_mastermind.marketpace_openstack.create_template_for_plan',
        )

        signals.post_save.connect(
            handlers.synchronize_plan_component,
            sender=marketplace_models.PlanComponent,
            dispatch_uid='waldur_mastermind.marketpace_openstack.synchronize_plan_component',
        )

        FIXED = marketplace_models.OfferingComponent.BillingTypes.FIXED
        manager.register(offering_type=PACKAGE_TYPE,
                         create_resource_processor=processors.PackageCreateProcessor,
                         update_resource_processor=processors.PackageUpdateProcessor,
                         delete_resource_processor=processors.PackageDeleteProcessor,
                         components=(
                             Component(type=RAM_TYPE, name='RAM', measured_unit='GB', billing_type=FIXED),
                             Component(type=CORES_TYPE, name='Cores', measured_unit='cores', billing_type=FIXED),
                             Component(type=STORAGE_TYPE, name='Storage', measured_unit='GB', billing_type=FIXED),
                         ),
                         service_type=OpenStackConfig.service_name,
                         secret_attributes=get_secret_attributes)

        manager.register(offering_type=INSTANCE_TYPE,
                         create_resource_processor=processors.InstanceCreateProcessor,
                         delete_resource_processor=processors.InstanceDeleteProcessor)

        manager.register(offering_type=VOLUME_TYPE,
                         create_resource_processor=processors.VolumeCreateProcessor,
                         delete_resource_processor=processors.VolumeDeleteProcessor)

        signals.post_save.connect(
            handlers.synchronize_volume_metadata,
            sender=tenant_models.Volume,
            dispatch_uid='waldur_mastermind.marketpace_openstack.synchronize_volume_metadata',
        )

        signals.post_save.connect(
            handlers.synchronize_instance_metadata,
            sender=tenant_models.Instance,
            dispatch_uid='waldur_mastermind.marketpace_openstack.synchronize_instance_metadata',
        )

        signals.post_save.connect(
            handlers.synchronize_internal_ips,
            sender=tenant_models.InternalIP,
            dispatch_uid='waldur_mastermind.marketpace_openstack.synchronize_internal_ips',
        )

        signals.post_save.connect(
            handlers.synchronize_floating_ips,
            sender=tenant_models.FloatingIP,
            dispatch_uid='waldur_mastermind.marketpace_openstack.synchronize_floating_ips',
        )
