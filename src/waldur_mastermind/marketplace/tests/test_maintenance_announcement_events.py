from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import MaintenanceState
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class MaintenanceAnnouncementEventsTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.announcement = self.fixture.maintenance_announcement
        self.client.force_authenticate(self.fixture.staff)

    def _get_events_for_announcement(self, event_type):
        return logging_models.Event.objects.filter(
            event_type=event_type,
            context__maintenance_announcement_uuid=self.announcement.uuid.hex,
        )

    def _assert_event_with_scope(self, event_type, expected_message_fragment):
        events = self._get_events_for_announcement(event_type)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertIn(expected_message_fragment, event.message)
        self.assertIn(self.announcement.name, event.message)

        feed = logging_models.Feed.objects.filter(
            event=event,
            content_type=ContentType.objects.get_for_model(self.announcement),
            object_id=self.announcement.id,
        )
        self.assertTrue(feed.exists())

        customer_feed = logging_models.Feed.objects.filter(
            event=event,
            content_type=ContentType.objects.get_for_model(
                self.announcement.service_provider.customer
            ),
            object_id=self.announcement.service_provider.customer.id,
        )
        self.assertTrue(customer_feed.exists())

    def test_create_emits_event(self):
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "name": "New maintenance",
                "message": "Test message",
                "scheduled_start": "2030-01-01T10:00:00Z",
                "scheduled_end": "2030-01-01T12:00:00Z",
                "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                    self.fixture.service_provider
                ),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.announcement = models.MaintenanceAnnouncement.objects.get(
            uuid=response.data["uuid"]
        )
        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_CREATED,
            "has been created",
        )
        event = self._get_events_for_announcement(
            EventType.MAINTENANCE_ANNOUNCEMENT_CREATED
        ).get()
        self.assertEqual(event.context.get("user_uuid"), self.fixture.staff.uuid.hex)

    def test_update_emits_event(self):
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )
        response = self.client.patch(url, {"message": "Updated maintenance message"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_UPDATED,
            "has been updated",
        )
        event = self._get_events_for_announcement(
            EventType.MAINTENANCE_ANNOUNCEMENT_UPDATED
        ).get()
        self.assertIn("message:", event.message)
        self.assertEqual(event.context.get("user_uuid"), self.fixture.staff.uuid.hex)

    def test_delete_emits_event(self):
        announcement_name = self.announcement.name
        announcement_uuid = self.announcement.uuid.hex
        customer = self.announcement.service_provider.customer
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        events = logging_models.Event.objects.filter(
            event_type=EventType.MAINTENANCE_ANNOUNCEMENT_DELETED,
            context__maintenance_announcement_uuid=announcement_uuid,
        )
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertIn("has been deleted", event.message)
        self.assertIn(announcement_name, event.message)
        self.assertEqual(event.context.get("user_uuid"), self.fixture.staff.uuid.hex)

        # Maintenance scope Feed is gone with the object; customer scope remains.
        customer_feed = logging_models.Feed.objects.filter(
            event=event,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
        )
        self.assertTrue(customer_feed.exists())

    def test_schedule_emits_event(self):
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="schedule"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_SCHEDULED,
            "has been scheduled",
        )
        event = self._get_events_for_announcement(
            EventType.MAINTENANCE_ANNOUNCEMENT_SCHEDULED
        ).get()
        self.assertEqual(event.context.get("user_uuid"), self.fixture.staff.uuid.hex)

    def test_unschedule_emits_event(self):
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="unschedule"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_UNSCHEDULED,
            "has been unscheduled",
        )

    def test_start_emits_event(self):
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="start_maintenance"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_STARTED,
            "has been started",
        )

    def test_complete_emits_event(self):
        self.announcement.state = MaintenanceState.IN_PROGRESS
        self.announcement.save()
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="complete_maintenance"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_COMPLETED,
            "has been completed",
        )

    def test_cancel_from_draft_emits_event(self):
        self.announcement.state = MaintenanceState.DRAFT
        self.announcement.save()
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="cancel_maintenance"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_CANCELLED,
            "has been cancelled",
        )

    def test_cancel_from_scheduled_emits_event(self):
        self.announcement.state = MaintenanceState.SCHEDULED
        self.announcement.save()
        logging_models.Event.objects.all().delete()

        url = marketplace_factories.MaintenanceAnnouncementFactory.get_url(
            self.announcement, action="cancel_maintenance"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self._assert_event_with_scope(
            EventType.MAINTENANCE_ANNOUNCEMENT_CANCELLED,
            "has been cancelled",
        )
