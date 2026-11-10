"""Resource names show in remote sync event messages as written."""

from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote import tasks

TEXT = "AA{{BB}}CC {nosuchkey} {"


class EndDatePulledFromRemoteTest(test.APITestCase):
    def test_resource_name_shows_as_written(self):
        resource = marketplace_factories.ResourceFactory(name=TEXT)

        tasks.notify_resource_end_date_pulled_from_remote(
            resource.uuid.hex, "2025-01-01", "2026-01-01"
        )

        event = Event.objects.filter(
            event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED
        ).latest("created")
        self.assertEqual(
            event.message,
            f"End date of marketplace resource {TEXT} has been automatically "
            "updated from 2025-01-01 to 2026-01-01 (synced from remote allocation).",
        )
