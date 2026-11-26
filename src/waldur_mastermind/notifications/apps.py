from django.apps import AppConfig
from django.db.models import signals


class NotificationsConfig(AppConfig):
    name = "waldur_mastermind.notifications"
    verbose_name = "Notifications"

    def ready(self):
        from waldur_core.core import signals as core_signals
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.notifications import maintenance_handlers
        from waldur_mastermind.notifications.serializers import (
            AdminAnnouncementSerializer,
        )

        signals.post_save.connect(
            maintenance_handlers.manage_maintenance_admin_announcements,
            sender=marketplace_models.MaintenanceAnnouncement,
            dispatch_uid="waldur_mastermind.notifications.manage_maintenance_admin_announcements",
        )
        signals.post_save.connect(
            maintenance_handlers.update_maintenance_announcement_on_offering_change,
            sender=marketplace_models.MaintenanceAnnouncementOffering,
            dispatch_uid="waldur_mastermind.notifications.update_maintenance_announcement_on_offering_change",
        )
        signals.post_delete.connect(
            maintenance_handlers.update_maintenance_announcement_on_offering_change,
            sender=marketplace_models.MaintenanceAnnouncementOffering,
            dispatch_uid="waldur_mastermind.notifications.update_maintenance_announcement_on_offering_delete",
        )
        signals.pre_delete.connect(
            maintenance_handlers.cleanup_admin_announcement_on_maintenance_deletion,
            sender=marketplace_models.MaintenanceAnnouncement,
            dispatch_uid="waldur_mastermind.notifications.cleanup_admin_announcement_on_maintenance_deletion",
        )
        core_signals.pre_serializer_fields.connect(
            maintenance_handlers.add_maintenance_fields_to_admin_announcement_serializer,
            sender=AdminAnnouncementSerializer,
            dispatch_uid="waldur_mastermind.notifications.add_maintenance_fields_to_admin_announcement_serializer",
        )
