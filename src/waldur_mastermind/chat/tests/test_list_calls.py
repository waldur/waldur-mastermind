from django.test import TestCase

from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.chat.tools.proposals_researcher.list_calls import (
    ListCallsTool,
)
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures


class ListCallsToolExecuteTest(TestCase):
    def setUp(self):
        self.tool = ListCallsTool()
        self.fixture = proposal_fixtures.ProposalFixture()
        # Ensure call (active) and new_call (default state = draft) both exist.
        self.active_call = self.fixture.call
        self.draft_call = self.fixture.new_call

    def test_default_returns_only_active_calls(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "success")
        names = [c["name"] for c in result["data"]["calls"]]
        self.assertIn(self.active_call.name, names)
        self.assertNotIn(self.draft_call.name, names)

    def test_explicit_state_overrides_default(self):
        result = self.tool.execute(self.fixture.staff, {"state": "draft"})
        self.assertEqual(result["type"], "success")
        names = [c["name"] for c in result["data"]["calls"]]
        self.assertIn(self.draft_call.name, names)
        self.assertNotIn(self.active_call.name, names)

    def test_round_status_open_filters_to_open_rounds(self):
        # ProposalFixture.round is open by construction (start_time=now,
        # cutoff_time=now+10d) and lives on self.fixture.call (active).
        self.fixture.round
        result = self.tool.execute(self.fixture.staff, {"round_status": "open"})
        self.assertEqual(result["type"], "success")
        names = [c["name"] for c in result["data"]["calls"]]
        self.assertIn(self.active_call.name, names)

    def test_search_matches_name(self):
        result = self.tool.execute(
            self.fixture.staff, {"search": self.active_call.name[:4]}
        )
        self.assertEqual(result["type"], "success")
        names = [c["name"] for c in result["data"]["calls"]]
        self.assertIn(self.active_call.name, names)

    def test_invalid_state_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"state": "bogus"})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_manager_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"manager_uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_outsider_sees_no_calls(self):
        outsider = UserFactory()
        result = self.tool.execute(outsider, {})
        self.assertEqual(result["type"], "success")
        # filter_queryset_for_user returns an empty qs for users without
        # any role on the managing organisation/customer.
        self.assertEqual(result["data"]["calls"], [])

    def test_call_manager_sees_their_call(self):
        manager = self.fixture.call_manager
        result = self.tool.execute(manager, {})
        self.assertEqual(result["type"], "success")
        names = [c["name"] for c in result["data"]["calls"]]
        self.assertIn(self.active_call.name, names)
