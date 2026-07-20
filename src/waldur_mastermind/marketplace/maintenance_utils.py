"""Utilities for MaintenanceAnnouncement -> AdminAnnouncement integration."""

from django.utils import timezone
from django.utils.formats import date_format

from waldur_mastermind.marketplace.enums import MaintenanceType
from waldur_mastermind.notifications.models import AdminAnnouncement


class MaintenanceAnnouncementTemplate:
    """Simple template for generating AdminAnnouncement content from MaintenanceAnnouncement."""

    # Map maintenance types to display prefixes
    TYPE_PREFIXES = {
        MaintenanceType.EMERGENCY: "🚨 Emergency Maintenance",
        MaintenanceType.SCHEDULED: "🔧 Scheduled Maintenance",
        MaintenanceType.SECURITY: "🔒 Security Maintenance",
        MaintenanceType.UPGRADE: "⬆️ System Upgrade",
        MaintenanceType.PATCH: "🩹 Patch Deployment",
    }

    @classmethod
    def generate_announcement_content(cls, maintenance):
        """Generate simple AdminAnnouncement content using message as-is with type prefix."""
        prefix = cls.TYPE_PREFIXES.get(maintenance.maintenance_type, "🔧 Maintenance")
        return f"{prefix}: {maintenance.message}{cls._format_window(maintenance)}"

    @staticmethod
    def _format_window(maintenance):
        """Render the scheduled window, mirroring the public maintenance banner."""
        if not (maintenance.scheduled_start and maintenance.scheduled_end):
            return ""
        start, end = (
            date_format(timezone.localtime(value), "SHORT_DATETIME_FORMAT")
            for value in (maintenance.scheduled_start, maintenance.scheduled_end)
        )
        return f" (Scheduled {start} – {end})"

    @classmethod
    def get_announcement_priority(cls, maintenance):
        """Get AdminAnnouncement priority based on maintenance type."""
        # For now, use simple mapping based on type
        if maintenance.maintenance_type == MaintenanceType.EMERGENCY:
            return AdminAnnouncement.Type.DANGER
        elif maintenance.maintenance_type == MaintenanceType.SECURITY:
            return AdminAnnouncement.Type.WARNING
        else:  # SCHEDULED, UPGRADE, PATCH
            return AdminAnnouncement.Type.INFORMATION
