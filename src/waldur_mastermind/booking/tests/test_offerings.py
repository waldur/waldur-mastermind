from ddt import data, ddt
from django.db import connection as db_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.booking import models
from waldur_mastermind.marketplace.enums import BOOKING_OFFERING, OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal.enums import CallStates
from waldur_mastermind.proposal.tests import factories as proposal_factories

GOOGLE_CALENDAR_FIELDS = (
    "googlecalendar",
    "google_calendar_is_public",
    "google_calendar_link",
)


@ddt
class GoogleCalendarRemovedTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer,
            type=BOOKING_OFFERING,
            state=OfferingStates.ACTIVE,
        )
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        self.client.force_authenticate(self.fixture.staff)

    @data("google_calendar_sync", "share_google_calendar", "unshare_google_calendar")
    def test_calendar_actions_are_gone(self, action):
        response = self.client.post(
            f"/api/booking-offerings/{self.offering.uuid.hex}/{action}/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "/api/google-auth/",
        "/api/google-auth/callback/",
        "/api/google-auth/{uuid}/",
        "/api/google-auth/{uuid}/authorize/",
    )
    def test_google_auth_endpoints_are_gone(self, url):
        response = self.client.get(url.format(uuid=self.service_provider.uuid.hex))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_offering_responses_have_no_calendar_fields(self):
        for url in (
            marketplace_factories.OfferingFactory.get_url(self.offering),
            marketplace_factories.OfferingFactory.get_public_url(self.offering),
            reverse("booking-offering-detail", kwargs={"uuid": self.offering.uuid.hex}),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK, url)
            for field in GOOGLE_CALENDAR_FIELDS:
                self.assertNotIn(field, response.data, url)


class AdminWithoutGoogleCalendarTest(TestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory(type=BOOKING_OFFERING)
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )
        self.busy_slot = models.BusySlot.objects.create(
            offering=self.offering,
            start="2020-04-01T02:00:00+03:00",
            end="2020-04-02T02:00:00+03:00",
        )
        self.client.force_login(
            structure_factories.UserFactory(is_staff=True, is_superuser=True)
        )

    def test_change_pages_load(self):
        for url in (
            reverse("admin:marketplace_offering_change", args=[self.offering.pk]),
            reverse(
                "admin:marketplace_serviceprovider_change",
                args=[self.service_provider.pk],
            ),
            reverse("admin:booking_busyslot_changelist"),
            reverse("admin:booking_busyslot_change", args=[self.busy_slot.pk]),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_200_OK, url)
            self.assertNotContains(response, "Google calendar")
            self.assertNotContains(response, "Google credentials")


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
