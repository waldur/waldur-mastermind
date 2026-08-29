"""``attributes.prepaid_duration_months`` on a resource request is validated.

Allocation derives the project length from it (``utils.project_end_date``), so
it is held to the offering's prepaid terms — min, max and step — exactly as the
marketplace order path holds the same length. It is not capped against the
call's ``fixed_duration_in_days``: the subscription outranks the call.
"""

from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
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

    def test_the_subscription_is_not_capped_by_the_call(self):
        self.fixture.call.fixed_duration_in_days = 90
        self.fixture.call.save()

        response = self.create(prepaid_duration_months=12)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

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
