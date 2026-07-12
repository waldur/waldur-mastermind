import datetime

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.tests import factories, fixtures


class ReviewerRoleExpiryConcealmentTest(test.APITestCase):
    """Role expiration is team-admin metadata and must be hidden from reviewers
    viewing the proposal team; call managers and staff still see it."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(self.proposal, action="list_users")
        # A team member carrying an expiration time, so there is something to hide.
        self.proposal.add_user(
            structure_factories.UserFactory(),
            ProposalRole.MEMBER,
            expiration_time=timezone.now() + datetime.timedelta(days=30),
        )

    def _results(self, response):
        data = response.data
        return data["results"] if isinstance(data, dict) else data

    def _has_expiration_key(self, response):
        return any("expiration_time" in item for item in self._results(response))

    def test_reviewer_does_not_see_role_expiration(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self._has_expiration_key(response))

    def test_call_manager_sees_role_expiration(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self._has_expiration_key(response))
