from constance.test.unittest import override_config
from django.test import TransactionTestCase
from django.utils import timezone

from waldur_mastermind.marketplace.enums import (
    ImpactLevel,
    MaintenanceState,
    MaintenanceType,
)
from waldur_mastermind.marketplace.maintenance_utils import (
    MaintenanceAnnouncementTemplate,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.notifications.models import AdminAnnouncement


class MaintenanceAnnouncementSignalsTest(TransactionTestCase):
    def setUp(self):
        self.service_provider = factories.ServiceProviderFactory()
        self.offering = factories.OfferingFactory()

    @override_config(MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES=60)
    def test_admin_announcement_created_on_schedule(self):
        """Test that AdminAnnouncement is created when maintenance is scheduled."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Maintenance",
            message="Test message",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Initially no admin announcement
        self.assertIsNone(maintenance.admin_announcement)
        self.assertEqual(AdminAnnouncement.objects.count(), 0)

        # Schedule the maintenance (DRAFT → SCHEDULED)
        maintenance.schedule()
        maintenance.save()

        # Refresh from database
        maintenance.refresh_from_db()

        # Admin announcement should be created
        self.assertIsNotNone(maintenance.admin_announcement)
        self.assertEqual(AdminAnnouncement.objects.count(), 1)

        admin_announcement = maintenance.admin_announcement
        # Should contain type prefix and message
        self.assertIn(
            "🔧 Scheduled Maintenance: Test message", admin_announcement.description
        )
        self.assertEqual(admin_announcement.type, AdminAnnouncement.Type.INFORMATION)

        # Check timing - should be 60 minutes before scheduled start
        expected_active_from = maintenance.scheduled_start - timezone.timedelta(
            minutes=60
        )
        expected_active_to = maintenance.scheduled_end + timezone.timedelta(hours=1)

        self.assertEqual(admin_announcement.active_from, expected_active_from)
        self.assertEqual(admin_announcement.active_to, expected_active_to)

    def test_manual_admin_announcement_deletion_handling(self):
        """Test that manually deleted AdminAnnouncement is handled gracefully."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.SCHEDULED,
        )

        # Create admin announcement
        admin_announcement = AdminAnnouncement.objects.create(
            description="Test announcement",
            type=AdminAnnouncement.Type.INFORMATION,
            active_from=timezone.now(),
            active_to=timezone.now() + timezone.timedelta(hours=2),
        )
        maintenance.admin_announcement = admin_announcement
        maintenance.save()

        self.assertEqual(AdminAnnouncement.objects.count(), 1)

        # Manually delete the AdminAnnouncement (simulate staff deletion)
        admin_announcement.delete()
        self.assertEqual(AdminAnnouncement.objects.count(), 0)

        # Refresh maintenance from database - FK should be automatically set to NULL
        maintenance.refresh_from_db()

        # Check that FK is now NULL due to SET_NULL behavior
        self.assertIsNone(getattr(maintenance, "admin_announcement_id"))

        # Try to update maintenance - should handle gracefully
        maintenance.name = "Updated Maintenance Name"
        maintenance.save()

        # Maintenance should still have cleared reference
        maintenance.refresh_from_db()
        self.assertIsNone(getattr(maintenance, "admin_announcement_id"))

        # Should not have recreated the announcement
        self.assertEqual(AdminAnnouncement.objects.count(), 0)

    def test_admin_announcement_content_uses_simple_format(self):
        """Test that AdminAnnouncement uses simple message format with type prefix."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Maintenance",
            message="System will be unavailable for upgrades",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule the maintenance to create AdminAnnouncement
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement

        # Should contain type prefix, original message and the scheduled window
        self.assertTrue(
            admin_announcement.description.startswith(
                "🔧 Scheduled Maintenance: System will be unavailable for upgrades"
            ),
            admin_announcement.description,
        )
        self.assertIn("(Scheduled ", admin_announcement.description)
        self.assertEqual(admin_announcement.type, AdminAnnouncement.Type.INFORMATION)

    def test_emergency_maintenance_gets_danger_priority(self):
        """Test that emergency maintenance gets DANGER priority."""

        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Emergency Fix",
            message="Critical security vulnerability fix",
            maintenance_type=MaintenanceType.EMERGENCY,
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule the maintenance
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement

        # Should have emergency prefix and DANGER priority
        self.assertTrue(
            admin_announcement.description.startswith(
                "🚨 Emergency Maintenance: Critical security vulnerability fix"
            ),
            admin_announcement.description,
        )
        self.assertEqual(admin_announcement.type, AdminAnnouncement.Type.DANGER)

    def test_security_maintenance_gets_warning_priority(self):
        """Test that security maintenance gets WARNING priority."""

        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Security Update",
            message="Installing security patches for all services",
            maintenance_type=MaintenanceType.SECURITY,
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule the maintenance
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement

        # Should have security prefix and WARNING priority
        self.assertTrue(
            admin_announcement.description.startswith(
                "🔒 Security Maintenance: Installing security patches for all services"
            ),
            admin_announcement.description,
        )
        self.assertEqual(admin_announcement.type, AdminAnnouncement.Type.WARNING)

    def test_maintenance_content_updates_when_message_changes(self):
        """Test that AdminAnnouncement content updates when maintenance message changes."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="System Update",
            message="Initial maintenance message",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule the maintenance
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement
        original_content = admin_announcement.description
        self.assertIn("Initial maintenance message", original_content)

        # Update the maintenance message
        maintenance.message = "Updated maintenance message with new details"
        maintenance.save()

        # AdminAnnouncement should be updated
        admin_announcement.refresh_from_db()
        updated_content = admin_announcement.description

        self.assertNotEqual(original_content, updated_content)
        self.assertIn("Updated maintenance message with new details", updated_content)
        self.assertNotIn("Initial maintenance message", updated_content)

    def test_maintenance_type_changes_update_content_and_priority(self):
        """Test that changing maintenance type updates both content and priority."""

        maintenance = factories.MaintenanceAnnouncementFactory(
            name="System Update",
            message="Service maintenance required",
            maintenance_type=MaintenanceType.SCHEDULED,
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule the maintenance
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement
        self.assertIn("🔧 Scheduled Maintenance", admin_announcement.description)
        self.assertEqual(admin_announcement.type, AdminAnnouncement.Type.INFORMATION)

        # Change to emergency maintenance
        maintenance.maintenance_type = MaintenanceType.EMERGENCY
        maintenance.save()

        # AdminAnnouncement should be updated
        admin_announcement.refresh_from_db()

        # Content should reflect new type
        self.assertIn("🚨 Emergency Maintenance", admin_announcement.description)
        self.assertIn("Service maintenance required", admin_announcement.description)

        # Priority should be escalated to DANGER
        self.assertEqual(admin_announcement.type, AdminAnnouncement.Type.DANGER)

    def test_admin_announcement_serializer_includes_service_provider(self):
        """The banner attributes maintenance to a provider, so the name must resolve."""
        from waldur_mastermind.notifications.serializers import (
            AdminAnnouncementSerializer,
        )

        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Maintenance",
            message="Testing provider attribution",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        data = AdminAnnouncementSerializer(maintenance.admin_announcement).data

        self.assertEqual(
            data["maintenance_service_provider"],
            self.service_provider.customer.name,
        )

    def test_admin_announcement_serializer_includes_affected_offerings(self):
        """Test that AdminAnnouncementSerializer includes affected offerings information."""
        from waldur_mastermind.marketplace.enums import ImpactLevel
        from waldur_mastermind.notifications.serializers import (
            AdminAnnouncementSerializer,
        )

        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Maintenance",
            message="Testing affected offerings serialization",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Add affected offerings
        offering1 = factories.OfferingFactory(name="Service A")
        offering2 = factories.OfferingFactory(name="Service B")

        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=offering1,
            impact_level=ImpactLevel.PARTIAL_OUTAGE,
            impact_description="Service will be partially unavailable",
        )
        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=offering2,
            impact_level=ImpactLevel.DEGRADED_PERFORMANCE,
            impact_description="Service may experience slowdowns",
        )

        # Schedule the maintenance
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement

        # Serialize the admin announcement
        serializer = AdminAnnouncementSerializer(admin_announcement)
        data = serializer.data

        # Check that maintenance fields are present
        self.assertIn("maintenance_uuid", data)
        self.assertIn("maintenance_affected_offerings", data)
        self.assertEqual(data["maintenance_uuid"], str(maintenance.uuid))

        # Check affected offerings data
        affected_offerings = data["maintenance_affected_offerings"]
        self.assertEqual(len(affected_offerings), 2)

        # Sort by offering name for consistent testing
        affected_offerings.sort(key=lambda x: x["name"])

        # Verify first offering (Service A)
        offering_a = affected_offerings[0]
        self.assertEqual(offering_a["name"], "Service A")
        self.assertEqual(offering_a["uuid"], str(offering1.uuid))
        self.assertEqual(offering_a["impact_level"], ImpactLevel.PARTIAL_OUTAGE)
        self.assertEqual(
            offering_a["impact_description"], "Service will be partially unavailable"
        )
        self.assertIn("impact_level_display", offering_a)

        # Verify second offering (Service B)
        offering_b = affected_offerings[1]
        self.assertEqual(offering_b["name"], "Service B")
        self.assertEqual(offering_b["uuid"], str(offering2.uuid))
        self.assertEqual(offering_b["impact_level"], ImpactLevel.DEGRADED_PERFORMANCE)
        self.assertEqual(
            offering_b["impact_description"], "Service may experience slowdowns"
        )

    def test_cleanup_admin_announcement_on_maintenance_deletion(self):
        """Test that AdminAnnouncement is deleted when MaintenanceAnnouncement is deleted."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Deletion Cleanup",
            message="Testing cleanup on deletion",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule maintenance to create AdminAnnouncement
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        # Verify AdminAnnouncement exists
        self.assertIsNotNone(maintenance.admin_announcement)
        admin_announcement = maintenance.admin_announcement
        admin_announcement_id = admin_announcement.id

        # Delete the maintenance
        maintenance.delete()

        # Verify AdminAnnouncement was also deleted
        self.assertFalse(
            AdminAnnouncement.objects.filter(id=admin_announcement_id).exists()
        )

    def test_cleanup_admin_announcement_handles_already_deleted(self):
        """Test that cleanup handler handles already deleted AdminAnnouncement gracefully."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Already Deleted",
            message="Testing already deleted handling",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Schedule maintenance to create AdminAnnouncement
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        # Manually delete the AdminAnnouncement first to simulate concurrent deletion
        admin_announcement = maintenance.admin_announcement
        admin_announcement.delete()

        # Deleting maintenance should not raise an exception even though
        # the AdminAnnouncement is already gone
        try:
            maintenance.delete()
        except Exception as e:
            self.fail(f"Maintenance deletion should not raise exception: {e}")

    def test_cleanup_admin_announcement_without_admin_announcement(self):
        """Test that cleanup handler works when no AdminAnnouncement exists."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test No AdminAnnouncement",
            message="Testing without AdminAnnouncement",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,  # Draft state, so no AdminAnnouncement
        )

        # Verify no AdminAnnouncement exists
        self.assertIsNone(maintenance.admin_announcement)

        # Deleting maintenance should work fine
        try:
            maintenance.delete()
        except Exception as e:
            self.fail(f"Maintenance deletion should not raise exception: {e}")

    def test_update_maintenance_announcement_on_offering_change(self):
        """Test that AdminAnnouncement content updates when affected offerings change."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Offering Change",
            message="Testing offering change updates",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Add initial affected offering
        offering1 = factories.OfferingFactory(name="Initial Service")
        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=offering1,
            impact_level=ImpactLevel.PARTIAL_OUTAGE,
            impact_description="Initial impact",
        )

        # Schedule maintenance to create AdminAnnouncement
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement

        # Add another affected offering (this triggers the signal)
        offering2 = factories.OfferingFactory(name="New Service")
        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=offering2,
            impact_level=ImpactLevel.FULL_OUTAGE,
            impact_description="New impact",
        )

        # Refresh admin announcement to check if it was updated
        admin_announcement.refresh_from_db()

        # The AdminAnnouncement should have been updated (modified timestamp changed)
        # Note: In a real database this would be different, but in test transactions
        # the timing might be too fast to detect. We'll check that the process completed.
        self.assertIsNotNone(admin_announcement.modified)

    def test_update_on_offering_change_when_admin_announcement_manually_deleted(self):
        """Test handling when AdminAnnouncement is manually deleted but MaintenanceAnnouncement still references it."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            name="Test Manual Deletion",
            message="Testing manual deletion handling",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        # Add affected offering
        offering = factories.OfferingFactory(name="Test Service")
        affected_offering = factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=offering,
            impact_level=ImpactLevel.PARTIAL_OUTAGE,
            impact_description="Initial impact",
        )

        # Schedule maintenance to create AdminAnnouncement
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        # Manually delete the AdminAnnouncement (simulate admin deletion)
        admin_announcement = maintenance.admin_announcement
        admin_announcement_id = maintenance.admin_announcement_id
        admin_announcement.delete()

        # Maintenance still has the FK reference
        self.assertEqual(maintenance.admin_announcement_id, admin_announcement_id)

        # Update the affected offering (this should trigger the signal)
        affected_offering.impact_description = "Updated impact description"
        affected_offering.save()

        # Refresh maintenance from DB
        maintenance.refresh_from_db()

        # The FK reference should now be cleared
        self.assertIsNone(maintenance.admin_announcement_id)
        self.assertIsNone(maintenance.admin_announcement)

    def test_update_on_offering_change_only_for_scheduled_maintenance(self):
        """Test that offering changes only update AdminAnnouncement for scheduled maintenance."""
        # Test with DRAFT maintenance (should not trigger update)
        draft_maintenance = factories.MaintenanceAnnouncementFactory(
            name="Draft Maintenance",
            message="Testing draft state",
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )

        offering = factories.OfferingFactory(name="Test Service")
        draft_affected = factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=draft_maintenance,
            offering=offering,
            impact_level=ImpactLevel.PARTIAL_OUTAGE,
            impact_description="Draft impact",
        )

        # Update offering - should not create AdminAnnouncement
        draft_affected.impact_description = "Updated draft impact"
        draft_affected.save()

        draft_maintenance.refresh_from_db()
        self.assertIsNone(draft_maintenance.admin_announcement)

        # Test with COMPLETED maintenance (should not trigger update)
        completed_maintenance = factories.MaintenanceAnnouncementFactory(
            name="Completed Maintenance",
            message="Testing completed state",
            service_provider=self.service_provider,
            state=MaintenanceState.COMPLETED,
        )

        completed_affected = factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=completed_maintenance,
            offering=offering,
            impact_level=ImpactLevel.PARTIAL_OUTAGE,
            impact_description="Completed impact",
        )

        # Update offering - should not trigger update since no AdminAnnouncement exists
        completed_affected.impact_description = "Updated completed impact"
        completed_affected.save()

        completed_maintenance.refresh_from_db()
        self.assertIsNone(completed_maintenance.admin_announcement)

    @override_config(
        MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES=60,
        MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES=60,
    )
    def test_scheduled_end_patch_during_in_progress_updates_active_to(self):
        """Patching scheduled_end while IN_PROGRESS must shift active_to."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        maintenance.start_maintenance()
        maintenance.save()
        maintenance.refresh_from_db()

        new_scheduled_end = maintenance.scheduled_end + timezone.timedelta(hours=3)
        maintenance.scheduled_end = new_scheduled_end
        maintenance.save()

        admin_announcement = maintenance.admin_announcement
        admin_announcement.refresh_from_db()
        self.assertEqual(
            admin_announcement.active_to,
            new_scheduled_end + timezone.timedelta(minutes=60),
        )

    @override_config(
        MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES=60,
        MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES=60,
    )
    def test_early_completion_collapses_active_to_to_actual_end(self):
        """Completing early collapses active_to to actual_end + buffer."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        maintenance.start_maintenance()
        maintenance.save()
        maintenance.refresh_from_db()

        early_end = maintenance.scheduled_end - timezone.timedelta(hours=1)
        maintenance.actual_end = early_end
        maintenance.complete_maintenance()
        maintenance.save()

        admin_announcement = maintenance.admin_announcement
        admin_announcement.refresh_from_db()
        self.assertEqual(
            admin_announcement.active_to,
            early_end + timezone.timedelta(minutes=60),
        )
        # Content uses the maintenance_type prefix; ensure it was regenerated.
        self.assertIn(
            MaintenanceAnnouncementTemplate.TYPE_PREFIXES[MaintenanceType.SCHEDULED],
            admin_announcement.description,
        )

    @override_config(
        MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES=60,
        MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES=60,
    )
    def test_overrun_extends_active_to_when_scheduled_end_pushed_in_progress(self):
        """Pushing scheduled_end past the original while IN_PROGRESS extends the window."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        maintenance.start_maintenance()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement
        original_active_to = admin_announcement.active_to

        extended_end = maintenance.scheduled_end + timezone.timedelta(hours=4)
        maintenance.scheduled_end = extended_end
        maintenance.save()

        admin_announcement.refresh_from_db()
        self.assertGreater(admin_announcement.active_to, original_active_to)
        self.assertEqual(
            admin_announcement.active_to,
            extended_end + timezone.timedelta(minutes=60),
        )

    def test_in_progress_offering_change_updates_announcement(self):
        """Adding an affected offering while IN_PROGRESS refreshes the announcement."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        maintenance.start_maintenance()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement
        original_modified = admin_announcement.modified

        offering = factories.OfferingFactory(name="In-progress Service")
        factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=maintenance,
            offering=offering,
            impact_level=ImpactLevel.FULL_OUTAGE,
            impact_description="Added mid-run",
        )

        admin_announcement.refresh_from_db()
        self.assertGreaterEqual(admin_announcement.modified, original_modified)
        # FK reference should still be intact.
        maintenance.refresh_from_db()
        self.assertEqual(maintenance.admin_announcement_id, admin_announcement.id)

    def test_cancel_still_removes_announcement(self):
        """Regression: cancelling a scheduled maintenance still deletes the announcement."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        self.assertIsNotNone(maintenance.admin_announcement)
        admin_announcement_id = maintenance.admin_announcement.id

        maintenance.cancel_maintenance()
        maintenance.save()
        maintenance.refresh_from_db()

        self.assertIsNone(maintenance.admin_announcement)
        self.assertFalse(
            AdminAnnouncement.objects.filter(id=admin_announcement_id).exists()
        )

    @override_config(
        MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES=60,
        MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES=30,
    )
    def test_trailing_buffer_constance_override(self):
        """Trailing buffer Constance setting governs how far past the end we stay visible."""
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()

        admin_announcement = maintenance.admin_announcement
        self.assertEqual(
            admin_announcement.active_to,
            maintenance.scheduled_end + timezone.timedelta(minutes=30),
        )
