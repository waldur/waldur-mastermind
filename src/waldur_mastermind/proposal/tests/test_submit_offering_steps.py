"""Submitting to a call whose workflow works per requested offering."""

from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


@mock.patch("waldur_mastermind.proposal.views.tasks")
class SubmitWithOfferingStepsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        self.call = self.proposal.round.call
        # The seeded technical assessment, enabled: one slot per offering.
        self.step = self.call.workflow_steps.get(key="technical_assessment")
        self.step.is_enabled = True
        self.step.per_requested_offering = True
        self.step.save()
        # The fixture's proposal comes asking for a resource; this one asks for
        # nothing until a test says otherwise.
        self.proposal.requestedresource_set.all().delete()
        self.url = factories.ProposalFactory.get_url(self.proposal, "submit")
        self.client.force_authenticate(self.proposal.created_by)

    def test_nothing_requested_is_refused(self, tasks):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Request at least one resource", str(response.data))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    def test_a_requested_resource_is_enough(self, tasks):
        factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
            limits={"cpu": 10},
        )
        response = self.client.post(self.url)
        self.assertNotIn("Request at least one resource", str(response.data))

    def test_a_call_assessing_nothing_per_offering_takes_it(self, tasks):
        self.step.is_enabled = False
        self.step.save()
        response = self.client.post(self.url)
        self.assertNotIn("Request at least one resource", str(response.data))
