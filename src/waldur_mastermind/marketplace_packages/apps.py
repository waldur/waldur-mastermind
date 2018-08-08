from django.apps import AppConfig
from django.db.models import signals


class MarketplacePackageConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_packages'
    verbose_name = 'Marketplace VPC packages'

    def ready(self):
        from waldur_mastermind.packages import models as package_models
        from waldur_mastermind.marketplace.plugins import manager

        from . import handlers, processor

        signals.post_save.connect(
            handlers.create_offering_for_package_template,
            sender=package_models.PackageTemplate,
            dispatch_uid='waldur_mastermind.marketpace_packages.'
                         'create_offering_for_package_template',
        )

        signals.post_save.connect(
            handlers.sync_offering_attribute_with_template_component,
            sender=package_models.PackageComponent,
            dispatch_uid='waldur_mastermind.marketpace_packages.'
                         'sync_offering_attribute_with_template_component',
        )

        manager.register(package_models.PackageTemplate, processor.process_order_item)
