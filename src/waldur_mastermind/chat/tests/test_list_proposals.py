from django.test import TestCase

from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.chat.tools.proposals_researcher.list_proposals import (
    ListProposalsTool,
)
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures


class ListProposalsToolExecuteTest(TestCase):
    def setUp(self):
        self.tool = ListProposalsTool()
        self.fixture = proposal_fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal_submitted = self.fixture.proposal_submitted

    def test_staff_sees_all_proposals(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "success")
        slugs = [p["slug"] for p in result["data"]["proposals"]]
        self.assertIn(self.proposal.slug, slugs)
        self.assertIn(self.proposal_submitted.slug, slugs)

    def test_filter_by_call_name(self):
        result = self.tool.execute(
            self.fixture.staff, {"call_name": self.fixture.call.name}
        )
        self.assertEqual(result["type"], "success")
        slugs = [p["slug"] for p in result["data"]["proposals"]]
        self.assertIn(self.proposal.slug, slugs)

    def test_filter_by_state(self):
        result = self.tool.execute(self.fixture.staff, {"state": ["submitted"]})
        self.assertEqual(result["type"], "success")
        states = {p["state"] for p in result["data"]["proposals"]}
        self.assertEqual(states, {"submitted"})

    def test_mine_filter_scopes_to_creator(self):
        creator = self.fixture.proposal_submitted_creator
        result = self.tool.execute(creator, {"mine": True})
        self.assertEqual(result["type"], "success")
        slugs = [p["slug"] for p in result["data"]["proposals"]]
        self.assertIn(self.proposal_submitted.slug, slugs)
        # The other proposal has a different creator.
        self.assertNotIn(self.proposal.slug, slugs)

    def test_invalid_call_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"call_uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_state_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"state": ["bogus_state"]})
        self.assertEqual(result["type"], "validation_error")

    def test_outsider_sees_nothing(self):
        outsider = UserFactory()
        result = self.tool.execute(outsider, {})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["proposals"], [])

    def test_creator_sees_own_proposal(self):
        creator = self.fixture.proposal_submitted_creator
        result = self.tool.execute(creator, {})
        self.assertEqual(result["type"], "success")
        slugs = [p["slug"] for p in result["data"]["proposals"]]
        self.assertIn(self.proposal_submitted.slug, slugs)

    def test_call_manager_sees_call_proposals(self):
        # ProposalFixture.call_manager has CallRole.MANAGER on the call,
        # which carries LIST_PROPOSALS via the fixture's __init__.
        manager = self.fixture.call_manager
        result = self.tool.execute(manager, {})
        self.assertEqual(result["type"], "success")
        slugs = [p["slug"] for p in result["data"]["proposals"]]
        self.assertIn(self.proposal_submitted.slug, slugs)
