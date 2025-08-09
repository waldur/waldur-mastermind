"""
Tests for MaintenanceAnnouncement filtering functionality.
"""

from datetime import UTC

from ddt import ddt
from rest_framework import status, test

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class MaintenanceAnnouncementFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.url = factories.MaintenanceAnnouncementFactory.get_list_url()

    def test_service_provider_filter(self):
        """Test filtering by service provider UUID."""
        # Create an announcement with a different service provider
        other_service_provider = factories.ServiceProviderFactory()
        other_announcement = factories.MaintenanceAnnouncementFactory(
            service_provider=other_service_provider
        )

        # Test filtering by the fixture's service provider
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"service_provider_uuid": str(self.fixture.service_provider.uuid)}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(self.announcement.uuid), uuids)
        self.assertNotIn(str(other_announcement.uuid), uuids)

    def test_maintenance_type_filter(self):
        """Test filtering by maintenance type."""
        # Create announcements with different maintenance types
        scheduled_announcement = factories.MaintenanceAnnouncementFactory(
            maintenance_type=models.MaintenanceType.SCHEDULED,
            service_provider=self.fixture.service_provider,
        )
        emergency_announcement = factories.MaintenanceAnnouncementFactory(
            maintenance_type=models.MaintenanceType.EMERGENCY,
            service_provider=self.fixture.service_provider,
        )

        self.client.force_authenticate(self.fixture.staff)

        # Filter by scheduled maintenance
        response = self.client.get(
            self.url, {"maintenance_type": models.MaintenanceType.SCHEDULED}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(scheduled_announcement.uuid), uuids)
        self.assertNotIn(str(emergency_announcement.uuid), uuids)

    def test_state_filter(self):
        """Test filtering by maintenance state."""
        # Create announcements in different states
        draft_announcement = factories.MaintenanceAnnouncementFactory(
            service_provider=self.fixture.service_provider
        )  # Default state is DRAFT

        self.client.force_authenticate(self.fixture.staff)

        # Filter by draft state (using string representation)
        response = self.client.get(self.url, {"state": "Draft"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(draft_announcement.uuid), uuids)

    def test_date_range_filters(self):
        """Test filtering by scheduled start/end date ranges."""
        from datetime import datetime, timedelta

        base_date = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

        # Create announcements with different scheduled dates
        early_announcement = factories.MaintenanceAnnouncementFactory(
            scheduled_start=base_date,
            scheduled_end=base_date + timedelta(hours=2),
            service_provider=self.fixture.service_provider,
        )
        late_announcement = factories.MaintenanceAnnouncementFactory(
            scheduled_start=base_date + timedelta(days=10),
            scheduled_end=base_date + timedelta(days=10, hours=2),
            service_provider=self.fixture.service_provider,
        )

        self.client.force_authenticate(self.fixture.staff)

        # Filter by scheduled_start_after
        filter_date = base_date + timedelta(days=5)
        response = self.client.get(
            self.url, {"scheduled_start_after": filter_date.isoformat()}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertNotIn(str(early_announcement.uuid), uuids)
        self.assertIn(str(late_announcement.uuid), uuids)

    def test_ordering(self):
        """Test ordering functionality."""
        # Create announcements with different names for consistent ordering
        factories.MaintenanceAnnouncementFactory(
            name="A Maintenance", service_provider=self.fixture.service_provider
        )
        factories.MaintenanceAnnouncementFactory(
            name="Z Maintenance", service_provider=self.fixture.service_provider
        )

        self.client.force_authenticate(self.fixture.staff)

        # Test ordering by name ascending
        response = self.client.get(self.url, {"o": "name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        names = [
            item["name"]
            for item in response.data
            if item["name"] in ["A Maintenance", "Z Maintenance"]
        ]
        if len(names) >= 2:
            self.assertTrue(
                names[0] < names[1], f"Expected ascending order, got {names}"
            )

    def test_combined_filters(self):
        """Test combining multiple filters."""
        # Create announcements for testing
        matching_announcement = factories.MaintenanceAnnouncementFactory(
            name="Matching Maintenance",
            maintenance_type=models.MaintenanceType.SCHEDULED,
            service_provider=self.fixture.service_provider,
        )
        non_matching_announcement = factories.MaintenanceAnnouncementFactory(
            name="Non-matching Maintenance",
            maintenance_type=models.MaintenanceType.EMERGENCY,
            service_provider=self.fixture.service_provider,
        )

        self.client.force_authenticate(self.fixture.staff)

        # Filter by both service provider and maintenance type
        response = self.client.get(
            self.url,
            {
                "service_provider_uuid": str(self.fixture.service_provider.uuid),
                "maintenance_type": models.MaintenanceType.SCHEDULED,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(matching_announcement.uuid), uuids)
        self.assertNotIn(str(non_matching_announcement.uuid), uuids)
