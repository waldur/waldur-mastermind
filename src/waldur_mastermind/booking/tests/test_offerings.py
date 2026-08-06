import re
from unittest import mock

from ddt import data, ddt
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.booking import models
from waldur_mastermind.google.tests import factories as google_factories
from waldur_mastermind.marketplace.enums import (
    BOOKING_OFFERING,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal.enums import CallStates
from waldur_mastermind.proposal.tests import factories as proposal_factories

from .. import calendar


@ddt
class BookingOfferingActionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer,
            type=BOOKING_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.PlanFactory(offering=self.offering)
        self.google_calendar = google_factories.GoogleCalendarFactory(
            offering=self.offering
        )
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )
        self.google_credentials = google_factories.GoogleCredentialsFactory(
            service_provider=self.service_provider,
            calendar_token="calendar_token",
            calendar_refresh_token="calendar_refresh_token",
        )
        self.schedules = [
            {
                "start": "2020-02-12T02:00:00+03:00",
                "end": "2020-02-15T02:00:00+03:00",
            },
            {
                "start": "2020-03-01T02:00:00+03:00",
                "end": "2020-03-05T02:00:00+03:00",
            },
        ]

        self.resource_1 = marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            attributes={"schedules": [self.schedules[0]]},
        )

        self.resource_2 = marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            attributes={"schedules": [self.schedules[1]]},
        )

        self.slot_1 = models.BookingSlot.objects.create(
            resource=self.resource_1,
            start=self.schedules[0]["start"],
            end=self.schedules[0]["end"],
        )

        self.slot_2 = models.BookingSlot.objects.create(
            resource=self.resource_2,
            start=self.schedules[1]["start"],
            end=self.schedules[1]["end"],
        )

    @data("owner", "staff")
    def test_user_can_sync_bookings_to_calendar(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            f"/api/booking-offerings/{self.offering.uuid.hex}/google_calendar_sync/"
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data("user")
    def test_user_cannot_sync_bookings_to_calendar(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            f"/api/booking-offerings/{self.offering.uuid.hex}/google_calendar_sync/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_google_calendar_sync_validators(self):
        self.google_credentials.delete()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            f"/api/booking-offerings/{self.offering.uuid.hex}/google_calendar_sync/"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.google.backend.build")
    @freeze_time("2020-02-20")
    def test_bookings_sync(self, mock_build):
        mock_build().events().list().execute().get.return_value = []
        sync_bookings = calendar.SyncBookings(self.offering)

        need_to_add, need_to_delete, need_to_update, _ = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 2)
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 0)

        mock_build().events().list().execute().get.return_value = [
            {
                "start": {"dateTime": self.slot_2.start},
                "end": {"dateTime": self.slot_2.end},
                "id": self.slot_2.backend_id,
            }
        ]

        self.slot_2.start = "2020-03-02T02:00:00+03:00"
        self.slot_2.save()
        need_to_add, need_to_delete, need_to_update, _ = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 1)
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 1)
        self.assertEqual(
            need_to_update[0].id, re.sub(r"[^a-z0-9]", "", self.slot_2.backend_id)
        )

        # Past events are also being updated
        self.slot_2.start = "2020-02-02T02:00:00+03:00"
        self.slot_2.save()
        need_to_add, need_to_delete, need_to_update, _ = sync_bookings.get_bookings()
        self.assertEqual(len(need_to_add), 1)
        self.assertEqual(len(need_to_delete), 0)
        self.assertEqual(len(need_to_update), 1)
        self.assertEqual(
            need_to_update[0].id, re.sub(r"[^a-z0-9]", "", self.slot_2.backend_id)
        )

    @mock.patch("waldur_mastermind.google.backend.build")
    def test_automatically_create_google_calendar(self, mock_build):
        # if calendar backend_id exists
        backend = calendar.SyncBookings(self.offering)
        backend.calendar_id
        mock_build().calendars().insert().execute.assert_not_called()

        # if calendar backend_id doesn't exist
        self.google_calendar.backend_id = ""
        self.google_calendar.save()
        backend = calendar.SyncBookings(self.offering)
        mock_build().calendars().insert().execute.return_value = {
            "id": "new_calendar_id"
        }
        backend.calendar_id
        mock_build().calendars().insert().execute.assert_called_once()
        self.google_calendar.refresh_from_db()
        self.assertEqual(self.google_calendar.backend_id, "new_calendar_id")

    @mock.patch("waldur_mastermind.booking.handlers.GoogleCalendarRenameExecutor")
    def test_update_google_calendar_name_if_offering_name_has_been_updated(
        self, mock_executor
    ):
        self.offering.name = "new name"
        self.offering.save()
        mock_executor.execute.assert_called_once()

    def test_marketplace_offering_serializer_has_calendar_info(self):
        self.client.force_authenticate(self.fixture.staff)

        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("google_calendar_is_public" in response.data.keys())
        self.assertEqual(
            response.data["google_calendar_is_public"], self.google_calendar.public
        )

        url = marketplace_factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("google_calendar_is_public" in response.data.keys())
        self.assertEqual(
            response.data["google_calendar_is_public"], self.google_calendar.public
        )

    def test_marketplace_offering_serializer_has_google_calendar_link(self):
        self.client.force_authenticate(self.fixture.staff)

        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("google_calendar_link" in response.data.keys())
        self.assertEqual(
            response.data["google_calendar_link"], self.google_calendar.http_link
        )

        url = marketplace_factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("google_calendar_link" in response.data.keys())
        self.assertEqual(
            response.data["google_calendar_link"], self.google_calendar.http_link
        )


class BookingOfferingOpenForProposalsTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("booking-offering-list")
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def create_offering(self):
        offering = marketplace_factories.OfferingFactory(
            type=BOOKING_OFFERING, state=OfferingStates.ACTIVE
        )
        requested_offering = proposal_factories.RequestedOfferingFactory(
            offering=offering, call__state=CallStates.ACTIVE
        )
        proposal_factories.RoundFactory(call=requested_offering.call, opened=True)
        return offering

    def test_list_does_not_run_a_query_per_offering(self):
        query = {"field": ["uuid", "open_for_proposals"]}
        self.create_offering()

        # Warm up one-off lookups so the measurements differ only in row count.
        self.client.get(self.url, query)

        with CaptureQueriesContext(db_connection) as ctx_one:
            self.client.get(self.url, query)

        for _ in range(3):
            self.create_offering()

        with CaptureQueriesContext(db_connection) as ctx_many:
            response = self.client.get(self.url, query)

        self.assertEqual(len(response.data), 4)
        self.assertTrue(all(row["open_for_proposals"] for row in response.data))
        self.assertEqual(len(ctx_one), len(ctx_many))

    def test_annotation_resolves_per_offering(self):
        self.create_offering()
        marketplace_factories.OfferingFactory(
            type=BOOKING_OFFERING, state=OfferingStates.ACTIVE
        )

        response = self.client.get(self.url, {"field": ["uuid", "open_for_proposals"]})
        self.assertEqual(
            sorted(row["open_for_proposals"] for row in response.data), [False, True]
        )
