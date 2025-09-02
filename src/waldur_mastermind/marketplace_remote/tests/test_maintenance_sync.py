import uuid
from datetime import timedelta

import respx
from django.test import testcases
from django.utils import timezone
from rest_framework import status

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    ImpactLevel,
    MaintenanceState,
    MaintenanceType,
)
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_remote import tasks


class MaintenanceAnnouncementSyncTest(testcases.TransactionTestCase):
    def setUp(self):
        respx.start()
        self.remote_customer_uuid = uuid.uuid4().hex
        self.remote_user_uuid = uuid.uuid4().hex
        self.remote_api_token = uuid.uuid4().hex

        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider

        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.api_url = "https://example.com"
        self.offering.secret_options = {
            "api_url": self.api_url,
            "token": self.remote_api_token,
            "customer_uuid": self.remote_customer_uuid,
        }
        self.offering.save()

        self.mock_api_response("customers/", [])
        self.mock_api_response("projects/", [])
        self.remote_maintenance_uuid = uuid.uuid4()

    def tearDown(self):
        respx.stop()
        respx.reset()

    def mock_api_response(
        self, endpoint, response_data, status_code=status.HTTP_200_OK
    ):
        """Helper to mock API responses."""
        respx.get(
            f"{self.api_url}/api/{endpoint}",
        ).mock(return_value=respx.MockResponse(status_code, json=response_data))

    def create_remote_maintenance_data(self, **overrides):
        """Helper to create remote maintenance announcement data."""
        base_data = {
            "url": f"{self.api_url}/api/maintenance-announcements/{uuid.uuid4().hex}/",
            "uuid": self.remote_maintenance_uuid.hex,
            "name": "Database Maintenance",
            "state": "Scheduled",
            "scheduled_start": "2025-08-18T00:00:00Z",
            "scheduled_end": "2025-08-18T02:00:00Z",
            "actual_start": None,
            "actual_end": None,
            "service_provider": f"{self.api_url}/api/marketplace-service-providers/{self.remote_customer_uuid}/",
            "created_by": None,
            "affected_offerings": [],
            "service_provider_name": "Test Provider",
            "message": "Scheduled database maintenance",
            "maintenance_type": 1,
            "external_reference_url": "",
            "backend_id": self.remote_maintenance_uuid.hex,
        }
        base_data.update(overrides)
        return base_data

    def test_sync_new_maintenance_announcement(self):
        """Test syncing a new maintenance announcement from remote."""
        remote_maintenance = self.create_remote_maintenance_data()

        self.mock_api_response("maintenance-announcements/", [remote_maintenance])

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(self.service_provider)

        local_maintenance = models.MaintenanceAnnouncement.objects.get(
            service_provider=self.service_provider, name="Database Maintenance"
        )
        self.assertEqual(local_maintenance.state, MaintenanceState.SCHEDULED)
        self.assertEqual(local_maintenance.message, "Scheduled database maintenance")
        self.assertEqual(local_maintenance.maintenance_type, MaintenanceType.SCHEDULED)

    def test_sync_update_existing_maintenance_announcement(self):
        """Test updating an existing maintenance announcement."""
        existing_maintenance = models.MaintenanceAnnouncement.objects.create(
            service_provider=self.service_provider,
            name="Database Maintenance",
            message="Old message",
            backend_id=self.remote_maintenance_uuid.hex,
            maintenance_type=MaintenanceType.SCHEDULED,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=2),
            state=MaintenanceState.DRAFT,
        )
        remote_maintenance = self.create_remote_maintenance_data(
            name="Database Maintenance",
            message="Updated maintenance message",
            state="In progress",
        )

        self.mock_api_response("maintenance-announcements/", [remote_maintenance])

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(self.service_provider)

        existing_maintenance.refresh_from_db()
        self.assertEqual(existing_maintenance.message, "Updated maintenance message")
        self.assertEqual(existing_maintenance.state, MaintenanceState.IN_PROGRESS)

    def test_sync_delete_stale_maintenance_announcement(self):
        """Test deleting maintenance announcements that no longer exist remotely."""
        stale_maintenance = models.MaintenanceAnnouncement.objects.create(
            service_provider=self.service_provider,
            name="Stale Maintenance",
            message="This should be deleted",
            backend_id=uuid.uuid4().hex,
            maintenance_type=MaintenanceType.SCHEDULED,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=2),
            state=MaintenanceState.DRAFT,
        )

        self.mock_api_response("maintenance-announcements/", [])

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(self.service_provider)

        with self.assertRaises(models.MaintenanceAnnouncement.DoesNotExist):
            stale_maintenance.refresh_from_db()

    def test_sync_with_affected_offerings(self):
        """Test syncing maintenance announcement with affected offerings."""
        affected_offering = models.Offering.objects.create(
            customer=self.service_provider.customer,
            name="Affected Service",
            category=self.offering.category,
            type="OpenStack.Instance",
        )

        remote_maintenance = self.create_remote_maintenance_data(
            affected_offerings=[
                {
                    "url": f"{self.api_url}/api/maintenance-announcement-offerings/{uuid.uuid4().hex}/",
                    "uuid": str(uuid.uuid4()),
                    "maintenance": f"{self.api_url}/api/maintenance-announcements/{uuid.uuid4().hex}/",
                    "offering": f"{self.api_url}/api/offerings/{uuid.uuid4().hex}/",
                    "impact_level": 3,
                    "impact_level_display": "Partial outage",
                    "offering_name": "Affected Service",
                    "impact_description": "Service will be temporarily unavailable",
                }
            ]
        )

        self.mock_api_response("maintenance-announcements/", [remote_maintenance])

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(self.service_provider)

        local_maintenance = models.MaintenanceAnnouncement.objects.get(
            service_provider=self.service_provider, name="Database Maintenance"
        )
        affected_offering_rel = local_maintenance.affected_offerings.first()
        self.assertIsNotNone(affected_offering_rel)
        self.assertEqual(affected_offering_rel.offering, affected_offering)
        self.assertEqual(affected_offering_rel.impact_level, ImpactLevel.PARTIAL_OUTAGE)
        self.assertEqual(
            affected_offering_rel.impact_description,
            "Service will be temporarily unavailable",
        )

    def test_sync_skips_non_remote_service_provider(self):
        """Test that sync skips service providers without remote offerings."""
        non_remote_sp = models.ServiceProvider.objects.create(
            customer=self.fixture.customer, description="Non-remote provider"
        )

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(non_remote_sp)

        self.assertEqual(
            models.MaintenanceAnnouncement.objects.filter(
                service_provider=non_remote_sp
            ).count(),
            0,
        )

    def test_sync_handles_offering_not_found_gracefully(self):
        """Test that sync handles missing offerings gracefully."""
        remote_maintenance = self.create_remote_maintenance_data(
            affected_offerings=[
                {
                    "url": f"{self.api_url}/api/maintenance-announcement-offerings/{uuid.uuid4().hex}/",
                    "uuid": str(uuid.uuid4()),
                    "maintenance": f"{self.api_url}/api/maintenance-announcements/{uuid.uuid4().hex}/",
                    "offering": f"{self.api_url}/api/offerings/{uuid.uuid4().hex}/",
                    "impact_level": 2,
                    "impact_level_display": "Degraded performance",
                    "offering_name": "Missing Service",
                    "impact_description": "Some impact",
                }
            ]
        )

        self.mock_api_response("maintenance-announcements/", [remote_maintenance])

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(self.service_provider)

        # Assert - maintenance created but no affected offerings
        local_maintenance = models.MaintenanceAnnouncement.objects.get(
            service_provider=self.service_provider, name="Database Maintenance"
        )
        self.assertEqual(local_maintenance.affected_offerings.count(), 0)

    def test_maintenance_announcement_list_pull_task(self):
        """Test the list pull task that orchestrates sync for all service providers."""
        remote_maintenance = self.create_remote_maintenance_data()
        self.mock_api_response("maintenance-announcements/", [remote_maintenance])

        task = tasks.MaintenanceAnnouncementListPullTask()
        pulled_objects = task.get_pulled_objects()

        self.assertIn(self.service_provider, pulled_objects)

        pull_task = tasks.MaintenanceAnnouncementPullTask()
        pull_task.pull(self.service_provider)

        local_maintenance = models.MaintenanceAnnouncement.objects.get(
            service_provider=self.service_provider, name="Database Maintenance"
        )
        self.assertEqual(local_maintenance.state, MaintenanceState.SCHEDULED)

    def test_sync_updates_correct_maintenance_by_name_and_type(self):
        """Test that sync updates the correct maintenance when name and maintenance type match."""
        existing_maintenance = models.MaintenanceAnnouncement.objects.create(
            service_provider=self.service_provider,
            name="Database Maintenance",
            message="Old message",
            maintenance_type=MaintenanceType.SCHEDULED,
            backend_id=self.remote_maintenance_uuid.hex,
            scheduled_start=timezone.datetime(2025, 8, 18, 0, 0, tzinfo=timezone.utc),
            scheduled_end=timezone.datetime(2025, 8, 18, 2, 0, tzinfo=timezone.utc),
            state=MaintenanceState.DRAFT,
        )

        remote_maintenance = self.create_remote_maintenance_data(
            uuid=self.remote_maintenance_uuid.hex,
            name="Database Maintenance",
            maintenance_type=1,
            message="Updated message",
            state="In progress",
        )

        self.mock_api_response("maintenance-announcements/", [remote_maintenance])

        task = tasks.MaintenanceAnnouncementPullTask()
        task.pull(self.service_provider)

        # Should still have only 1 maintenance (updated, not duplicated)
        maintenances = models.MaintenanceAnnouncement.objects.filter(
            service_provider=self.service_provider, name="Database Maintenance"
        )
        self.assertEqual(maintenances.count(), 1)

        existing_maintenance.refresh_from_db()
        self.assertEqual(existing_maintenance.message, "Updated message")
        self.assertEqual(existing_maintenance.state, MaintenanceState.IN_PROGRESS)
        self.assertEqual(
            existing_maintenance.maintenance_type, MaintenanceType.SCHEDULED
        )
