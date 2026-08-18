from django.apps import AppConfig
from django.conf import settings as django_settings
from django.db.models import signals


class MarketplaceOpenPortalConfig(AppConfig):
    name = "waldur_mastermind.marketplace_openportal"
    verbose_name = "Marketplace OpenPortal"
    service_name = "OpenPortal"

    def ready(self):
        # These need to be imported here to avoid circular imports
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
        from waldur_mastermind.marketplace.plugins import Component, manager
        from waldur_mastermind.marketplace_openportal import (
            PLUGIN_NAME,
            handlers,
            processor,
        )
        from waldur_openportal import models as openportal_models
        from waldur_openportal import signals as openportal_signals
        from waldur_openportal.apps import OpenPortalConfig

        signals.post_save.connect(
            handlers.update_component_quota,
            sender=openportal_models.Allocation,
            dispatch_uid="waldur_mastermind.marketplace_openportal.update_component_quota",
        )

        marketplace_handlers.connect_resource_handlers(openportal_models.Allocation)
        marketplace_handlers.connect_resource_metadata_handlers(
            openportal_models.Allocation
        )

        default_limits = django_settings.WALDUR_OPENPORTAL["DEFAULT_LIMITS"]

        manager.register(
            PLUGIN_NAME,
            create_resource_processor=processor.CreateAllocationProcessor,
            delete_resource_processor=processor.DeleteAllocationProcessor,
            can_update_limits=True,
            components=(
                Component(
                    type="node",
                    name="NODE",
                    measured_unit="hours",
                    billing_type=BillingTypes.USAGE,
                    limit_period=LimitPeriods.TOTAL,
                    limit_amount=default_limits["NODE"],
                ),
            ),
            service_type=OpenPortalConfig.service_name,
        )

        openportal_signals.openportal_association_created.connect(
            handlers.create_offering_user_for_openportal_user,
            sender=openportal_models.Allocation,
            dispatch_uid="waldur_mastermind.marketplace_openportal.create_offering_user_for_openportal_user",
        )

        openportal_signals.openportal_association_deleted.connect(
            handlers.drop_offering_user_for_openportal_user,
            sender=openportal_models.Allocation,
            dispatch_uid="waldur_mastermind.marketplace_openportal.drop_offering_user_for_openportal_user",
        )

        signals.post_save.connect(
            handlers.sync_component_user_usage_when_allocation_user_usage_is_submitted,
            sender=openportal_models.AllocationUserUsage,
            dispatch_uid="waldur_mastermind.marketplace_openportal.sync_component_user_usage_when_allocation_user_usage_is_submitted",
        )
