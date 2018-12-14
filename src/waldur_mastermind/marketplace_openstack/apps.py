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

        for index, model in enumerate((openstack_models.Tenant, tenant_models.Instance, tenant_models.Volume)):
            signals.post_save.connect(
                handlers.change_order_item_state,
                sender=model,
                dispatch_uid='waldur_mastermind.marketpace_openstack.change_order_item_state_%s' % index,
            )

            signals.pre_delete.connect(
                handlers.terminate_resource,
                sender=model,
                dispatch_uid='waldur_mastermind.marketpace_openstack.terminate_resource_%s' % index,
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
