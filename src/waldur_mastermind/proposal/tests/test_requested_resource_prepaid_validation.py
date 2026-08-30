"""``attributes.prepaid_duration_months`` on a resource request is validated.

It is held to the offering's prepaid terms — min, max and step — exactly as
the marketplace order path holds the same length, and to the call's
``fixed_duration_in_days``: that is the length of every project the call
awards, so no subscription requested under it may outlast it.
"""

import datetime

from ddt import data, ddt
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import AllocationTimes
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_prepaid_duration import PrepaidFixture

FIELD = "attributes.prepaid_duration_months"


@ddt
class RequestedResourcePrepaidValidationTest(PrepaidFixture, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.cpu.min_prepaid_duration = 3
        self.cpu.max_prepaid_duration = 12
        self.cpu.prepaid_duration_step = 3
        self.cpu.save()
        self.url = factories.RequestedResourceFactory.get_list_url(self.proposal)
        self.client.force_authenticate(self.fixture.proposal_creator)

    def create(self, **attributes):
        return self.client.post(
            self.url,
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "attributes": attributes,
                "limits": {"cpu_hours": 10},
            },
            format="json",
        )

    def test_a_length_within_the_terms_is_accepted(self):
        response = self.create(prepaid_duration_months=6)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_request_naming_no_length_is_accepted(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data(2, 24, 4, "abc", 0, -3, 6.5, True)
    def test_a_length_outside_the_terms_is_rejected(self, months):
        response = self.create(prepaid_duration_months=months)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)

    def test_a_length_on_an_offering_not_sold_by_the_month_is_rejected(self):
        self.cpu.is_prepaid = False
        self.cpu.save()

        response = self.create(prepaid_duration_months=6)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)
        self.assertIn("not sold by the month", str(response.data[FIELD]))

    def test_an_update_is_validated_too(self):
        requested_resource = self._request(None, prepaid_duration_months=6)
        url = factories.RequestedResourceFactory.get_url(
            self.proposal, requested_resource
        )

        response = self.client.patch(
            url, {"attributes": {"prepaid_duration_months": 24}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)
        requested_resource.refresh_from_db()
        self.assertEqual(requested_resource.attributes["prepaid_duration_months"], 6)

    def test_an_update_within_the_terms_is_accepted(self):
        requested_resource = self._request(None, prepaid_duration_months=6)
        url = factories.RequestedResourceFactory.get_url(
            self.proposal, requested_resource
        )

        response = self.client.patch(
            url, {"attributes": {"prepaid_duration_months": 9}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        requested_resource.refresh_from_db()
        self.assertEqual(requested_resource.attributes["prepaid_duration_months"], 9)

    def test_the_call_caps_a_length_the_offering_would_allow(self):
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()

        response = self.create(prepaid_duration_months=6)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)

    def test_the_component_terms_apply_per_prepaid_component(self):
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu_hours",
            is_prepaid=True,
            max_prepaid_duration=6,
        )

        response = self.create(prepaid_duration_months=9)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)


class FixedDurationCapTest(PrepaidFixture, test.APITestCase):
    """A call's fixed duration bounds the subscriptions requested under it."""

    def setUp(self):
        super().setUp()
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()
        self.url = factories.RequestedResourceFactory.get_list_url(self.proposal)
        self.client.force_authenticate(self.fixture.proposal_creator)

    def create(self, months):
        return self.client.post(
            self.url,
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "attributes": {"prepaid_duration_months": months},
                "limits": {"cpu_hours": 10},
            },
            format="json",
        )

    def schedule_allocation(self, date):
        """Date the round's allocation, as a fixed_date call does."""
        proposal_round = self.proposal.round
        proposal_round.allocation_date = timezone.make_aware(
            datetime.datetime.combine(date, datetime.time(hour=9))
        )
        proposal_round.save()
        models.CallWorkflowStep.objects.update_or_create(
            call=proposal_round.call,
            step="allocation_decision",
            defaults={"allocation_time": AllocationTimes.FIXED_DATE},
        )

    def test_four_months_never_fit_ninety_days(self):
        response = self.create(4)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)
        self.assertIn("90 days", str(response.data[FIELD]))

    def test_one_month_always_fits(self):
        response = self.create(1)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_three_months_fit_when_measured_from_a_short_stretch_of_the_year(self):
        # 2027-02-01 + 3 months = 2027-05-01, 89 days: inside the 90.
        self.schedule_allocation(datetime.date(2027, 2, 1))

        response = self.create(3)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_three_months_do_not_fit_when_measured_from_a_long_stretch(self):
        # 2027-03-01 + 3 months = 2027-06-01, 92 days: two months is the cap.
        self.schedule_allocation(datetime.date(2027, 3, 1))

        response = self.create(3)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)
        self.assertIn("at most 2 month", str(response.data[FIELD]))

    @freeze_time("2027-03-01")
    def test_a_call_allocating_on_decision_measures_from_today(self):
        # The round's date is ignored unless the call allocates on a fixed
        # date, so the bound is measured from today: three months from March
        # 1st do not fit, as above.
        self.proposal.round.allocation_date = timezone.now() - datetime.timedelta(
            days=28
        )
        self.proposal.round.save()

        response = self.create(3)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)

    def test_an_update_is_held_to_the_cap_too(self):
        requested_resource = self._request(None, prepaid_duration_months=1)
        url = factories.RequestedResourceFactory.get_url(
            self.proposal, requested_resource
        )

        response = self.client.patch(
            url, {"attributes": {"prepaid_duration_months": 4}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(FIELD, response.data)
        requested_resource.refresh_from_db()
        self.assertEqual(requested_resource.attributes["prepaid_duration_months"], 1)

    def test_no_cap_when_the_call_fixes_nothing(self):
        self.fixture.call.fixed_duration_in_days = None
        self.fixture.call.save()

        response = self.create(12)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
