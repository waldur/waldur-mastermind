from django.apps import AppConfig
from django.conf import settings as django_settings
from django.db.models import signals


class MarketplaceOpenPortalRemoteConfig(AppConfig):
    name = "waldur_mastermind.marketplace_openportal_remote"
    verbose_name = "Marketplace OpenPortal Remote"
    service_name = "OpenPortal Remote"

    def ready(self):
        # These need to be imported here to avoid circular imports
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
        from waldur_mastermind.marketplace.plugins import Component, manager
        from waldur_mastermind.marketplace_openportal_remote import (
            PLUGIN_NAME,
            handlers,
            processor,
        )
        from waldur_openportal import models as openportal_models
        from waldur_openportal import signals as openportal_signals
        from waldur_openportal.apps import OpenPortalConfig

        signals.post_save.connect(
            handlers.update_component_quota,
            sender=openportal_models.RemoteAllocation,
            dispatch_uid="waldur_mastermind.marketplace_openportal_remote.update_component_quota",
        )

        marketplace_handlers.connect_resource_handlers(
            openportal_models.RemoteAllocation
        )
        marketplace_handlers.connect_resource_metadata_handlers(
            openportal_models.RemoteAllocation
        )

        default_limits = django_settings.WALDUR_OPENPORTAL["DEFAULT_LIMITS"]

        manager.register(
            PLUGIN_NAME,
            create_resource_processor=processor.CreateRemoteAllocationProcessor,
            update_resource_processor=processor.UpdateRemoteAllocationLimitsProcessor,
            delete_resource_processor=processor.DeleteRemoteAllocationProcessor,
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

        openportal_signals.openportal_remote_association_created.connect(
            handlers.create_offering_user_for_openportal_remote_user,
            sender=openportal_models.RemoteAllocation,
            dispatch_uid="waldur_mastermind.marketplace_openportal_remote.create_offering_user_for_openportal_remote_user",
        )

        openportal_signals.openportal_remote_association_deleted.connect(
            handlers.drop_offering_user_for_openportal_remote_user,
            sender=openportal_models.RemoteAllocation,
            dispatch_uid="waldur_mastermind.marketplace_openportal_remote.drop_offering_user_for_openportal_remote_user",
        )

        signals.post_save.connect(
            handlers.sync_offering_resource_options,
            sender=marketplace_models.Offering,
            dispatch_uid="waldur_mastermind.marketplace_openportal_remote."
            "sync_offering_resource_options",
        )
