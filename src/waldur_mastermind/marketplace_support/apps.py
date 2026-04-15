from django.apps import AppConfig
from django.db.models import signals


class MarketplaceSupportConfig(AppConfig):
    name = "waldur_mastermind.marketplace_support"
    verbose_name = "Marketplace supports"

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_core.core.models import SshPublicKey
        from waldur_core.permissions.models import UserRole
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace import serializers as marketplace_serializers
        from waldur_mastermind.marketplace.enums import SUPPORT_OFFERING
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace_support.serializers import add_issue
        from waldur_mastermind.support import models as support_models

        from . import handlers, processor

        signals.post_save.connect(
            handlers.update_order_if_issue_was_complete,
            sender=support_models.Issue,
            dispatch_uid="waldur_mastermind.marketplace_support.update_order_if_issue_was_complete",
        )

        signals.post_save.connect(
            handlers.notify_about_request_based_item_creation,
            sender=support_models.Issue,
            dispatch_uid="waldur_mastermind.marketplace_support.notify_about_request_based_item_creation",
        )

        manager.register(
            SUPPORT_OFFERING,
            create_resource_processor=processor.CreateRequestProcessor,
            update_resource_processor=processor.UpdateRequestProcessor,
            delete_resource_processor=processor.DeleteRequestProcessor,
            can_terminate_order=True,
            enable_usage_notifications=True,
            enable_remote_support=True,
            can_update_limits=True,
        )

        core_signals.pre_serializer_fields.connect(
            sender=marketplace_serializers.OrderDetailsSerializer,
            receiver=add_issue,
        )

        signals.post_save.connect(
            handlers.create_issue_for_pending_support_order,
            sender=marketplace_models.Order,
            dispatch_uid="waldur_mastermind.marketplace_support.create_issue_for_pending_support_order",
        )

        signals.post_save.connect(
            handlers.create_issue_if_membership_changed,
            sender=UserRole,
            dispatch_uid="waldur_mastermind.marketplace_support.create_issue_if_membership_changed",
        )

        signals.post_save.connect(
            handlers.create_issue_if_ssh_key_added,
            sender=SshPublicKey,
            dispatch_uid="waldur_mastermind.marketplace_support.create_issue_if_ssh_key_added",
        )

        signals.post_delete.connect(
            handlers.create_issue_if_ssh_key_removed,
            sender=SshPublicKey,
            dispatch_uid="waldur_mastermind.marketplace_support.create_issue_if_ssh_key_removed",
        )
