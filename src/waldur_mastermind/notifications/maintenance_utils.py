"""Utilities for MaintenanceAnnouncement -> AdminAnnouncement integration."""

from django.utils import timezone
from django.utils.formats import date_format

from waldur_mastermind.marketplace.enums import MaintenanceState, MaintenanceType
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
    COMPLETED_ICON = "✅"

    @classmethod
    def generate_announcement_content(cls, maintenance):
        """Generate simple AdminAnnouncement content using message as-is with type prefix.

        A completed maintenance keeps the banner for a trailing buffer, so a
        trailing indicator makes it obvious the work is over.
        """
        prefix = cls.TYPE_PREFIXES.get(maintenance.maintenance_type, "🔧 Maintenance")
        content = f"{prefix}: {maintenance.message}{cls._format_window(maintenance)}"
        return f"{content}{cls._format_completion(maintenance)}"

    @classmethod
    def _format_completion(cls, maintenance):
        if maintenance.state != MaintenanceState.COMPLETED:
            return ""
        indicator = f"{cls.COMPLETED_ICON} Completed"
        if maintenance.actual_end:
            indicator += f" at {cls._format_datetime(maintenance.actual_end)}"
        return f" – {indicator}"

    @staticmethod
    def _format_datetime(value):
        return date_format(timezone.localtime(value), "SHORT_DATETIME_FORMAT")

    @classmethod
    def _format_window(cls, maintenance):
        """Render the scheduled window, mirroring the public maintenance banner."""
        if not (maintenance.scheduled_start and maintenance.scheduled_end):
            return ""
        start, end = (
            cls._format_datetime(value)
            for value in (maintenance.scheduled_start, maintenance.scheduled_end)
        )
        return f" (Scheduled {start} – {end})"

    @classmethod
    def get_announcement_priority(cls, maintenance):
        """Get AdminAnnouncement priority based on maintenance type."""
        # Once the work is done there is nothing left to warn about.
        if maintenance.state == MaintenanceState.COMPLETED:
            return AdminAnnouncement.Type.INFORMATION
        # For now, use simple mapping based on type
        if maintenance.maintenance_type == MaintenanceType.EMERGENCY:
            return AdminAnnouncement.Type.DANGER
        elif maintenance.maintenance_type == MaintenanceType.SECURITY:
            return AdminAnnouncement.Type.WARNING
        else:  # SCHEDULED, UPGRADE, PATCH
            return AdminAnnouncement.Type.INFORMATION
