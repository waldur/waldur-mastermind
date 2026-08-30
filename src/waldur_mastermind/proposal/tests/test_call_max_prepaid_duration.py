"""The call states the longest subscription its fixed duration admits.

``max_prepaid_duration_months`` is what the applicant's period selector is
bounded by; it and the request validation derive from one helper.
"""

import datetime

from freezegun import freeze_time
from rest_framework import status, test

from waldur_mastermind.proposal import utils
from waldur_mastermind.proposal.tests import factories, fixtures


class MaxPrepaidDurationHelperTest(test.APITestCase):
    def test_none_when_the_call_fixes_nothing(self):
        call = factories.CallFactory(fixed_duration_in_days=None)

        self.assertIsNone(
            utils.max_prepaid_duration_months(call, datetime.date(2027, 2, 1))
        )

    def test_the_answer_depends_on_the_anchor(self):
        call = factories.CallFactory(fixed_duration_in_days=90)

        # Feb 1 + 3 months = May 1, 89 days; Mar 1 + 3 months = Jun 1, 92 days.
        self.assertEqual(
            utils.max_prepaid_duration_months(call, datetime.date(2027, 2, 1)), 3
        )
        self.assertEqual(
            utils.max_prepaid_duration_months(call, datetime.date(2027, 3, 1)), 2
        )

    def test_a_duration_shorter_than_a_month_admits_none(self):
        call = factories.CallFactory(fixed_duration_in_days=20)

        self.assertEqual(
            utils.max_prepaid_duration_months(call, datetime.date(2027, 2, 1)), 0
        )

    def test_a_year_is_twelve_months(self):
        call = factories.CallFactory(fixed_duration_in_days=365)

        self.assertEqual(
            utils.max_prepaid_duration_months(call, datetime.date(2027, 2, 1)), 12
        )


@freeze_time("2027-02-01")
class CallSerializerMaxPrepaidDurationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_public_call_states_the_cap(self):
        self.call.fixed_duration_in_days = 90
        self.call.save()
        self.client.force_authenticate(self.fixture.user)

        response = self.client.get(factories.CallFactory.get_public_url(self.call))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["fixed_duration_in_days"], 90)
        self.assertEqual(response.data["max_prepaid_duration_months"], 3)

    def test_public_call_states_null_when_nothing_is_fixed(self):
        self.call.fixed_duration_in_days = None
        self.call.save()
        self.client.force_authenticate(self.fixture.user)

        response = self.client.get(factories.CallFactory.get_public_url(self.call))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["max_prepaid_duration_months"])

    def test_protected_call_states_the_cap_and_does_not_take_it(self):
        self.call.fixed_duration_in_days = 90
        self.call.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CallFactory.get_protected_url(self.call)

        response = self.client.get(url)
        self.assertEqual(response.data["max_prepaid_duration_months"], 3)

        # Read-only: derived from fixed_duration_in_days, never set directly.
        response = self.client.patch(
            url,
            {"fixed_duration_in_days": 365, "max_prepaid_duration_months": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["max_prepaid_duration_months"], 12)
