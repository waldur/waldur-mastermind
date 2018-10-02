from django.apps import AppConfig
from django.db.models import signals

from waldur_mastermind.marketplace.plugins import Component


class MarketplacePackageConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_packages'
    verbose_name = 'Marketplace VPC packages'

    def ready(self):
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_packages import PLUGIN_NAME
        from waldur_openstack.openstack import models as openstack_models
        from waldur_core.structure import models as structure_models

        from . import handlers, processor

        signals.post_save.connect(
            handlers.create_template_for_plan,
            sender=marketplace_models.Plan,
            dispatch_uid='waldur_mastermind.marketpace_packages.'
                         'create_template_for_plan',
        )

        signals.post_save.connect(
            handlers.synchronize_plan_component,
            sender=marketplace_models.PlanComponent,
            dispatch_uid='waldur_mastermind.marketpace_packages.'
                         'synchronize_plan_component',
        )

        signals.post_save.connect(
            handlers.change_order_item_state,
            sender=openstack_models.Tenant,
            dispatch_uid='waldur_mastermind.marketpace_packages.'
                         'change_order_item_state',
        )

        FIXED = marketplace_models.OfferingComponent.BillingTypes.FIXED
        manager.register(offering_type=PLUGIN_NAME,
                         processor=processor.process_order_item,
                         validator=processor.validate_order_item,
                         components=(
                             Component(type='ram', name='RAM', measured_unit='GB', billing_type=FIXED),
                             Component(type='cores', name='Cores', measured_unit='cores', billing_type=FIXED),
                             Component(type='storage', name='Storage', measured_unit='GB', billing_type=FIXED),
                         ),
                         scope_model=structure_models.ServiceSettings)
