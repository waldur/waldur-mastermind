from django.apps import AppConfig
from django.db.models import signals


class MarketplaceOpenStackConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_openstack'
    verbose_name = 'Marketplace OpenStack'

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_openstack.openstack import models as openstack_models
        from waldur_openstack.openstack_tenant import models as tenant_models
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace.plugins import Component

        from . import handlers, processors, INSTANCE_TYPE, VOLUME_TYPE, PACKAGE_TYPE

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

        marketplace_handlers.connect_resource_handlers(
            openstack_models.Tenant,
            tenant_models.Instance,
            tenant_models.Volume
        )

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
                             Component(type='ram', name='RAM', measured_unit='GB', billing_type=FIXED),
                             Component(type='cores', name='Cores', measured_unit='cores', billing_type=FIXED),
                             Component(type='storage', name='Storage', measured_unit='GB', billing_type=FIXED),
                         ),
                         scope_model=structure_models.ServiceSettings)

        manager.register(offering_type=INSTANCE_TYPE,
                         create_resource_processor=processors.InstanceCreateProcessor,
                         delete_resource_processor=processors.InstanceDeleteProcessor,
                         scope_model=structure_models.ServiceSettings)

        manager.register(offering_type=VOLUME_TYPE,
                         create_resource_processor=processors.VolumeCreateProcessor,
                         delete_resource_processor=processors.VolumeDeleteProcessor,
                         scope_model=structure_models.ServiceSettings)

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

        resource_models = (tenant_models.Instance, tenant_models.Volume, openstack_models.Tenant)
        for index, model in enumerate(resource_models):
            signals.post_save.connect(
                handlers.synchronize_resource_metadata,
                sender=model,
                dispatch_uid='waldur_mastermind.marketpace_openstack.'
                             'synchronize_resource_metadata_%s_%s' % (index, model.__class__),
            )
