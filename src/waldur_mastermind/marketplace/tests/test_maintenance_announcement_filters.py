"""
Tests for MaintenanceAnnouncement filtering functionality.
"""

from datetime import UTC, timedelta

from ddt import ddt
from django.utils import timezone
from rest_framework import status, test

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class MaintenanceAnnouncementFilterTest(test.APITestCase):
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


@ddt
class MaintenanceAnnouncementTimingFilterTest(test.APITestCase):
    """Ordering and filtering by the derived overrun / timing_bucket fields."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.maintenance_announcement.delete()
        self.sp = self.fixture.service_provider
        self.url = factories.MaintenanceAnnouncementFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)
        self.ss = timezone.now().replace(microsecond=0)
        self.se = self.ss + timedelta(hours=2)

    def _make(self, *, state, start_delta=None, end_delta=None):
        return factories.MaintenanceAnnouncementFactory(
            service_provider=self.sp,
            state=state,
            scheduled_start=self.ss,
            scheduled_end=self.se,
            actual_start=self.ss + timedelta(minutes=start_delta)
            if start_delta is not None
            else None,
            actual_end=self.se + timedelta(minutes=end_delta)
            if end_delta is not None
            else None,
        )

    def _ordered_uuids(self, ordering):
        response = self.client.get(self.url, {"o": ordering})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [item["uuid"] for item in response.data]

    def test_ordering_by_overrun_minutes(self):
        big = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=60
        )
        small = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=10
        )
        # Not completed -> overrun is NULL and must sink in both directions.
        pending = self._make(state=models.MaintenanceState.SCHEDULED)

        asc = self._ordered_uuids("overrun_minutes")
        self.assertLess(asc.index(str(small.uuid)), asc.index(str(big.uuid)))
        self.assertGreater(asc.index(str(pending.uuid)), asc.index(str(big.uuid)))

        desc = self._ordered_uuids("-overrun_minutes")
        self.assertLess(desc.index(str(big.uuid)), desc.index(str(small.uuid)))
        self.assertGreater(desc.index(str(pending.uuid)), desc.index(str(small.uuid)))

    def test_ordering_by_start_delta_minutes(self):
        late = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=60, end_delta=0
        )
        early = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=10, end_delta=0
        )
        # Not started -> start_delta is NULL and must sink in both directions.
        not_started = self._make(state=models.MaintenanceState.SCHEDULED)

        asc = self._ordered_uuids("start_delta_minutes")
        self.assertLess(asc.index(str(early.uuid)), asc.index(str(late.uuid)))
        self.assertGreater(asc.index(str(not_started.uuid)), asc.index(str(late.uuid)))

        desc = self._ordered_uuids("-start_delta_minutes")
        self.assertLess(desc.index(str(late.uuid)), desc.index(str(early.uuid)))
        self.assertGreater(
            desc.index(str(not_started.uuid)), desc.index(str(early.uuid))
        )

    def test_filter_by_timing_bucket_single(self):
        overrun = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=60
        )
        late = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=30, end_delta=0
        )
        on_time = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=0
        )

        response = self.client.get(self.url, {"timing_bucket": "overrun"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(overrun.uuid), uuids)
        self.assertNotIn(str(late.uuid), uuids)
        self.assertNotIn(str(on_time.uuid), uuids)

    def test_filter_by_timing_bucket_multi(self):
        overrun = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=60
        )
        late = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=30, end_delta=0
        )
        on_time = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=0
        )
        pending = self._make(state=models.MaintenanceState.SCHEDULED)

        response = self.client.get(self.url, {"timing_bucket": "overrun,late_start"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(overrun.uuid), uuids)
        self.assertIn(str(late.uuid), uuids)
        self.assertNotIn(str(on_time.uuid), uuids)
        self.assertNotIn(str(pending.uuid), uuids)

    def test_filter_by_timing_bucket_pending(self):
        pending = self._make(state=models.MaintenanceState.SCHEDULED)
        completed = self._make(
            state=models.MaintenanceState.COMPLETED, start_delta=0, end_delta=0
        )

        response = self.client.get(self.url, {"timing_bucket": "pending"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(str(pending.uuid), uuids)
        self.assertNotIn(str(completed.uuid), uuids)
