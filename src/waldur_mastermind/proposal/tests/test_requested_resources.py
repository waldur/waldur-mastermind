from ddt import data, ddt
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class RequestedResourceGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedResourceFactory.get_provider_list_url()

    @data(
        "staff",
        "offering_owner",
    )
    def test_request_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_request_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertFalse(len(response.json()))


class RequestedResourceOfferingFilterTest(test.APITestCase):
    """The offering is reached through RequestedOffering, not a direct FK.

    Filtering or ordering on the shorter ``offering__…`` path raised FieldError,
    which surfaced as a 500 rather than a validation error.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.url = factories.RequestedResourceFactory.get_provider_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_by_offering_uuid_returns_the_matching_row(self):
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.requested_resource.uuid.hex],
        )

    def test_filter_by_offering_uuid_excludes_other_offerings(self):
        other = marketplace_factories.OfferingFactory()
        response = self.client.get(self.url, {"offering_uuid": other.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_ordering_by_offering_name_is_accepted(self):
        response = self.client.get(self.url, {"o": "offering__name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class UserRequestedResourceGetTest(test.APITestCase):
    """ "My requests" — scoped to proposals the user can read, not offerings they manage."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.url = factories.RequestedResourceFactory.get_my_list_url()

    def get_as(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_applicant_sees_the_resource_they_requested(self):
        self.client.force_authenticate(self.fixture.proposal.created_by)
        response = self.client.get(self.url)
        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.requested_resource.uuid.hex],
        )

    def test_unrelated_user_sees_nothing(self):
        self.assertEqual(self.get_as("user"), [])

    def test_row_carries_both_lifecycles_separately(self):
        self.client.force_authenticate(self.fixture.staff)
        (row,) = self.client.get(self.url).json()
        self.assertEqual(row["proposal_state"], self.fixture.proposal.state)
        self.assertEqual(row["proposal_uuid"], self.fixture.proposal.uuid.hex)
        self.assertEqual(row["offering_uuid"], self.fixture.offering.uuid.hex)
        self.assertEqual(row["call_uuid"], self.fixture.call.uuid.hex)
        self.assertIn("created", row)

    def test_resource_state_is_null_until_the_resource_exists(self):
        self.requested_resource.resource = None
        self.requested_resource.save()

        self.client.force_authenticate(self.fixture.staff)
        (row,) = self.client.get(self.url).json()
        self.assertIsNone(row["resource_state"])
        self.assertIsNone(row["resource_name"])

    def test_resource_state_is_reported_once_provisioned(self):
        self.client.force_authenticate(self.fixture.staff)
        (row,) = self.client.get(self.url).json()
        self.assertEqual(
            row["resource_state"], self.requested_resource.resource.get_state_display()
        )

    def test_offering_filter_narrows_the_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(len(response.json()), 1)

        other = marketplace_factories.OfferingFactory()
        response = self.client.get(self.url, {"offering_uuid": other.uuid.hex})
        self.assertEqual(response.json(), [])

    def test_requests_on_dead_proposals_are_left_out(self):
        """Rejected and canceled proposals are settled questions.

        Listing them by default buries the requests the user can still act on.
        """
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(len(self.client.get(self.url).json()), 1)

        for state in (ProposalStates.REJECTED, ProposalStates.CANCELED):
            self.fixture.proposal.state = state
            self.fixture.proposal.save()
            self.assertEqual(
                self.client.get(self.url).json(), [], f"{state} should be hidden"
            )

    def test_dead_proposals_can_be_asked_for(self):
        self.fixture.proposal.state = ProposalStates.REJECTED
        self.fixture.proposal.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"include_closed": "true"})

        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.requested_resource.uuid.hex],
        )

    def test_list_does_not_run_a_query_per_row(self):
        self.client.force_authenticate(self.fixture.staff)

        # Warm up one-off lookups so the measurements differ only in row count.
        self.client.get(self.url)
        with CaptureQueriesContext(db_connection) as ctx_one:
            self.client.get(self.url)

        for _ in range(3):
            factories.RequestedResourceFactory(
                proposal=self.fixture.proposal,
                requested_offering=self.fixture.requested_offering_accepted,
            )

        with CaptureQueriesContext(db_connection) as ctx_many:
            response = self.client.get(self.url)

        self.assertEqual(len(response.json()), 4)
        self.assertEqual(len(ctx_one), len(ctx_many))
