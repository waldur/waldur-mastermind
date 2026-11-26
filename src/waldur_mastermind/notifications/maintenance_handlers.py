"""Signal handlers bridging marketplace MaintenanceAnnouncement to AdminAnnouncement."""

import logging
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.configuration import config
from waldur_mastermind.marketplace.enums import MaintenanceState
from waldur_mastermind.notifications.maintenance_utils import (
    MaintenanceAnnouncementTemplate,
)
from waldur_mastermind.notifications.models import AdminAnnouncement

logger = logging.getLogger(__name__)


def manage_maintenance_admin_announcements(sender, instance, created, **kwargs):
    """
    Manage AdminAnnouncement lifecycle based on MaintenanceAnnouncement state changes.

    Handles:
    - Creation when DRAFT → SCHEDULED
    - Cleanup when SCHEDULED → DRAFT (unschedule)
    - Cleanup when → CANCELLED
    - Refresh content/timing on SCHEDULED → IN_PROGRESS
    - Collapse window on IN_PROGRESS → COMPLETED
    - Update content when maintenance or affected offerings change
    """

    # Get previous state to detect transitions
    if not created and instance.tracker.has_changed("state"):
        old_state = instance.tracker.previous("state")
        new_state = instance.state

        # DRAFT → SCHEDULED: Create AdminAnnouncement
        if (
            old_state == MaintenanceState.DRAFT
            and new_state == MaintenanceState.SCHEDULED
        ):
            _create_maintenance_announcement(instance)

        # SCHEDULED → DRAFT: Remove AdminAnnouncement (unscheduled)
        elif (
            old_state == MaintenanceState.SCHEDULED
            and new_state == MaintenanceState.DRAFT
        ):
            _cleanup_maintenance_announcement(instance)

        # → CANCELLED: Remove AdminAnnouncement
        elif new_state == MaintenanceState.CANCELLED:
            _cleanup_maintenance_announcement(instance)

        # SCHEDULED → IN_PROGRESS: refresh content/window to use actual_start
        elif (
            old_state == MaintenanceState.SCHEDULED
            and new_state == MaintenanceState.IN_PROGRESS
            and _check_and_handle_missing_admin_announcement(instance)
        ):
            _update_maintenance_announcement_content(instance)

        # IN_PROGRESS → COMPLETED: collapse window to actual_end + buffer
        elif (
            old_state == MaintenanceState.IN_PROGRESS
            and new_state == MaintenanceState.COMPLETED
            and _check_and_handle_missing_admin_announcement(instance)
        ):
            _collapse_announcement_on_completion(instance)

    # Handle content/timing updates while maintenance is scheduled or in progress
    elif (
        not created
        and instance.state in (MaintenanceState.SCHEDULED, MaintenanceState.IN_PROGRESS)
        and _has_content_changes(instance)
        and _check_and_handle_missing_admin_announcement(instance)
    ):
        _update_maintenance_announcement_content(instance)


def _compute_announcement_window(maintenance):
    """Compute (active_from, active_to) for the announcement.

    Prefers actual_start/actual_end when set so an overrun or early finish is
    reflected. Honours the trailing buffer Constance setting so the banner
    stays visible briefly after completion.
    """
    notify_before_minutes = config.MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES
    trailing_buffer_minutes = int(
        config.MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES
    )

    start_reference = maintenance.actual_start or maintenance.scheduled_start
    active_from = start_reference - timezone.timedelta(minutes=notify_before_minutes)

    effective_end = maintenance.actual_end or maintenance.scheduled_end
    # During an overrun the scheduled_end may be in the past; keep the banner
    # visible until now() + buffer so it doesn't disappear before the operator
    # patches scheduled_end.
    if (
        maintenance.state == MaintenanceState.IN_PROGRESS
        and not maintenance.actual_end
        and timezone.now() > effective_end
    ):
        effective_end = max(effective_end, timezone.now())
    active_to = effective_end + timezone.timedelta(minutes=trailing_buffer_minutes)

    return active_from, active_to


def _create_maintenance_announcement(maintenance):
    """Create AdminAnnouncement with rich markdown content."""

    # Delete any existing announcement first (defensive)
    if maintenance.admin_announcement:
        maintenance.admin_announcement.delete()

    # Generate content
    content = MaintenanceAnnouncementTemplate.generate_announcement_content(maintenance)
    announcement_type = MaintenanceAnnouncementTemplate.get_announcement_priority(
        maintenance
    )

    active_from, active_to = _compute_announcement_window(maintenance)

    # Create announcement
    admin_announcement = AdminAnnouncement.objects.create(
        description=content,
        type=announcement_type,
        active_from=active_from,
        active_to=active_to,
    )

    _update_admin_announcement_reference(maintenance, admin_announcement)


def _cleanup_maintenance_announcement(maintenance):
    """Remove associated AdminAnnouncement."""
    if maintenance.admin_announcement:
        maintenance.admin_announcement.delete()
        _update_admin_announcement_reference(maintenance, None)


def _update_maintenance_announcement_content(maintenance):
    """Update AdminAnnouncement content and window from current maintenance state."""

    if not maintenance.admin_announcement:
        return

    try:
        # Regenerate content
        content = MaintenanceAnnouncementTemplate.generate_announcement_content(
            maintenance
        )
        announcement_type = MaintenanceAnnouncementTemplate.get_announcement_priority(
            maintenance
        )

        active_from, active_to = _compute_announcement_window(maintenance)

        maintenance.admin_announcement.description = content
        maintenance.admin_announcement.type = announcement_type
        maintenance.admin_announcement.active_from = active_from
        maintenance.admin_announcement.active_to = active_to
        maintenance.admin_announcement.save()

    except AdminAnnouncement.DoesNotExist:
        # AdminAnnouncement was manually deleted
        _clear_admin_announcement_reference(maintenance)


def _collapse_announcement_on_completion(maintenance):
    """Recompute window on completion so the banner clears after the buffer."""
    _update_maintenance_announcement_content(maintenance)


def _has_content_changes(maintenance):
    """Check if maintenance has changes that affect announcement content or window."""
    content_fields = [
        "name",
        "message",
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "maintenance_type",
    ]
    return any(maintenance.tracker.has_changed(field) for field in content_fields)


def _update_admin_announcement_reference(maintenance, admin_announcement=None):
    """
    Update the AdminAnnouncement reference without triggering signals.

    Args:
        maintenance: MaintenanceAnnouncement instance
        admin_announcement: AdminAnnouncement instance or None to clear
    """
    # Update using direct SQL to avoid triggering signals and potential recursion
    maintenance.__class__.objects.filter(pk=maintenance.pk).update(
        admin_announcement=admin_announcement
    )
    maintenance.admin_announcement = admin_announcement


def _clear_admin_announcement_reference(maintenance, reason="was manually deleted"):
    """
    Clear the AdminAnnouncement reference from maintenance.

    Args:
        maintenance: MaintenanceAnnouncement instance
        reason: Reason for clearing (used in log message)
    """
    logger.warning(
        f"AdminAnnouncement for maintenance {maintenance.uuid} {reason}. "
        f"Clearing reference and not regenerating."
    )
    _update_admin_announcement_reference(maintenance, None)


def _check_and_handle_missing_admin_announcement(maintenance, action="update"):
    """
    Check if AdminAnnouncement still exists and handle if missing.

    Args:
        maintenance: MaintenanceAnnouncement instance
        action: Action being performed (used in log message)

    Returns:
        bool: True if AdminAnnouncement exists, False if it was cleared
    """
    if not maintenance.admin_announcement_id:
        return False

    if not AdminAnnouncement.objects.filter(
        id=maintenance.admin_announcement_id
    ).exists():
        reason_msg = (
            "was manually deleted. Not regenerating unless maintenance is updated"
        )
        _clear_admin_announcement_reference(maintenance, reason_msg)
        return False

    return True


def update_maintenance_announcement_on_offering_change(sender, instance, **kwargs):
    """Update AdminAnnouncement when affected offerings change."""

    maintenance = instance.maintenance
    if maintenance.state in (
        MaintenanceState.SCHEDULED,
        MaintenanceState.IN_PROGRESS,
    ) and _check_and_handle_missing_admin_announcement(maintenance, "offering_change"):
        _update_maintenance_announcement_content(maintenance)


def cleanup_admin_announcement_on_maintenance_deletion(sender, instance, **kwargs):
    """Ensure AdminAnnouncement is cleaned up when MaintenanceAnnouncement is deleted."""
    if instance.admin_announcement:
        try:
            instance.admin_announcement.delete()
        except (ObjectDoesNotExist, AdminAnnouncement.DoesNotExist):
            # AdminAnnouncement was already deleted - this is fine
            logger.debug(
                f"AdminAnnouncement for maintenance {instance.uuid} was already deleted"
            )
        except Exception as e:
            # Log other unexpected exceptions but don't break the deletion process
            logger.warning(
                f"Failed to delete AdminAnnouncement for maintenance {instance.uuid}: {e}"
            )


def add_maintenance_fields_to_admin_announcement_serializer(sender, fields, **kwargs):
    """Add maintenance-related fields to AdminAnnouncementSerializer when maintenance is scheduled."""
    # Add maintenance fields if the AdminAnnouncement has a related MaintenanceAnnouncement
    fields["maintenance_uuid"] = serializers.SerializerMethodField()
    fields["maintenance_name"] = serializers.SerializerMethodField()
    fields["maintenance_type"] = serializers.SerializerMethodField()
    fields["maintenance_state"] = serializers.SerializerMethodField()
    fields["maintenance_scheduled_start"] = serializers.SerializerMethodField()
    fields["maintenance_scheduled_end"] = serializers.SerializerMethodField()
    fields["maintenance_service_provider"] = serializers.SerializerMethodField()
    fields["maintenance_affected_offerings"] = serializers.SerializerMethodField()
    fields["maintenance_external_reference_url"] = serializers.SerializerMethodField()

    # Add methods to the sender class (AdminAnnouncementSerializer)
    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_uuid(self, obj) -> str | None:
        try:
            return (
                str(obj.maintenance_announcement.uuid)
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_name(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.name
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_type(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.maintenance_type
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_state(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.state
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_maintenance_scheduled_start(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.scheduled_start
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_maintenance_scheduled_end(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.scheduled_end
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_service_provider(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.service_provider.customer.name
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                and obj.maintenance_announcement.service_provider
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "format": "uuid"},
                    "name": {"type": "string"},
                    "impact_level": {"type": "string"},
                    "impact_level_display": {"type": "string"},
                    "impact_description": {"type": "string"},
                },
            },
        }
    )
    def get_maintenance_affected_offerings(self, obj) -> list[dict[str, Any]]:
        try:
            if (
                not hasattr(obj, "maintenance_announcement")
                or not obj.maintenance_announcement
            ):
                return []

            affected_offerings = []
            for (
                affected_offering
            ) in obj.maintenance_announcement.affected_offerings.all():
                offering_info = {
                    "uuid": str(affected_offering.offering.uuid),
                    "name": affected_offering.offering.name,
                    "impact_level": affected_offering.impact_level,
                    "impact_level_display": affected_offering.get_impact_level_display(),
                    "impact_description": affected_offering.impact_description,
                }
                affected_offerings.append(offering_info)

            return affected_offerings
        except AttributeError:
            return []

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_external_reference_url(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.external_reference_url
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    # Add the methods to the serializer class
    sender.get_maintenance_uuid = get_maintenance_uuid
    sender.get_maintenance_name = get_maintenance_name
    sender.get_maintenance_type = get_maintenance_type
    sender.get_maintenance_state = get_maintenance_state
    sender.get_maintenance_scheduled_start = get_maintenance_scheduled_start
    sender.get_maintenance_scheduled_end = get_maintenance_scheduled_end
    sender.get_maintenance_service_provider = get_maintenance_service_provider
    sender.get_maintenance_affected_offerings = get_maintenance_affected_offerings
    sender.get_maintenance_external_reference_url = (
        get_maintenance_external_reference_url
    )
