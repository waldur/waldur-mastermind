"""What the AI assistant's proposal tools tell a provider of a proposal.

Someone who reads a proposal only as the manager of one of the offerings it
requests reads, through the proposal API, only their own offering's part of
it (``permissions.offering_scope``) and none of its reviews. The assistant's
tools give them no more than that; whoever reads the whole proposal gets the
whole overview, as before.
"""

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.proposals_researcher.proposal_overview import (
    ProposalOverviewTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.review_assistant import (
    ReviewAssistantTool,
)
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_provider_scope import (
    TwoProviderProposalBase,
)


class ProviderScopeTestBase(TwoProviderProposalBase):
    def setUp(self):
        super().setUp()
        self.reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.reviewer,
            state=models.Review.States.SUBMITTED,
            summary_score=7,
        )

    def _providers(self):
        return {
            "provider A": (self.manager_a, self.offering_a, self.resource_a),
            "provider B": (self.manager_b, self.offering_b, self.resource_b),
        }

    def _whole_readers(self):
        return {**super()._whole_readers(), "reviewer": self.reviewer}


class ProposalOverviewOfferingScopeTest(ProviderScopeTestBase):
    def _overview(self, user):
        result = ProposalOverviewTool().execute(user, {"uuid": self.proposal.uuid.hex})
        self.assertEqual(result["type"], "success", result)
        return result

    def test_a_provider_sees_only_its_own_offerings_request(self):
        for name, (manager, offering, resource) in self._providers().items():
            with self.subTest(name):
                data = self._overview(manager)["data"]
                self.assertEqual(
                    [
                        (row["offering"], row["limits"])
                        for row in data["resource_requests"]
                    ],
                    [(offering.name, resource.limits)],
                )
                self.assertTrue(data["reads_own_offerings_only"])

    def test_a_provider_sees_no_review_statistics(self):
        for name, (manager, _, _) in self._providers().items():
            with self.subTest(name):
                result = self._overview(manager)
                self.assertNotIn("reviews", result["data"])
                self.assertNotIn("reviews", result["summary"])

    def test_a_provider_sees_the_requests_the_proposal_api_shows_it(self):
        for name, (manager, _, _) in self._providers().items():
            with self.subTest(name):
                rows = self._get(manager, self._url("resources"))
                self.assertEqual(
                    {row["requested_offering"]["offering_name"] for row in rows},
                    {
                        row["offering"]
                        for row in self._overview(manager)["data"]["resource_requests"]
                    },
                )

    def test_whoever_reads_the_whole_proposal_sees_the_whole_overview(self):
        for name, user in self._whole_readers().items():
            with self.subTest(name):
                result = self._overview(user)
                data = result["data"]
                self.assertEqual(
                    {row["offering"] for row in data["resource_requests"]},
                    {self.offering_a.name, self.offering_b.name},
                )
                self.assertFalse(data["reads_own_offerings_only"])

    def test_the_call_team_and_reviewers_see_the_review_statistics(self):
        # The applicant team and the panel read no reviews through the API
        # before a decision; what the overview tells them of reviews is not
        # pinned here.
        for name, user in {
            "staff": self.staff,
            "call manager": self.call_manager,
            "reviewer": self.reviewer,
        }.items():
            with self.subTest(name):
                result = self._overview(user)
                self.assertEqual(result["data"]["reviews"]["total"], 1)
                self.assertEqual(result["data"]["reviews"]["completed"], 1)
                self.assertIn("1/1 reviews", result["summary"])


class ReviewAssistantOfferingScopeTest(ProviderScopeTestBase):
    def _assist(self, user):
        return ReviewAssistantTool().execute(user, {"uuid": self.proposal.uuid.hex})

    def _assignment_item(self, user, status):
        pool_entry = factories.CallReviewerPoolFactory(
            call=self.call, reviewer=factories.ReviewerProfileFactory(user=user)
        )
        return factories.AssignmentItemFactory(
            batch=factories.AssignmentBatchFactory(
                call=self.call,
                reviewer_pool_entry=pool_entry,
                status=AssignmentBatchStatuses.SENT,
            ),
            proposal=self.proposal,
            status=status,
        )

    def assertRefused(self, user):
        result = self._assist(user)
        self.assertEqual(result["type"], "error", result)
        self.assertNotIn("data", result)

    def assertAdmitted(self, user):
        result = self._assist(user)
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(
            {row["offering"] for row in result["data"]["resource_requests"]},
            {self.offering_a.name, self.offering_b.name},
        )

    def test_an_applicant_holding_a_rejected_review_is_refused(self):
        # A rejected review is no review work: the applicant team reads no
        # review of its proposal before the decision.
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.applicant,
            state=models.Review.States.REJECTED,
        )
        self.assertRefused(self.applicant)

    def test_a_panel_member_who_declined_an_assignment_is_refused(self):
        self._assignment_item(self.panel_member, AssignmentItemStatuses.DECLINED)
        self.assertRefused(self.panel_member)

    def test_a_live_reviewer_is_admitted(self):
        self.assertAdmitted(self.reviewer)

    def test_a_pool_reviewer_offered_the_proposal_is_admitted(self):
        user = structure_factories.UserFactory()
        self._assignment_item(user, AssignmentItemStatuses.PENDING)
        self.assertAdmitted(user)

    def test_staff_is_admitted(self):
        self.assertAdmitted(self.staff)

    def test_a_provider_whose_review_was_rejected_is_refused(self):
        # A rejected review no longer lets its reviewer read the proposal; the
        # manager of one of its offerings reads only that offering's part.
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.manager_a,
            state=models.Review.States.REJECTED,
        )
        self.assertRefused(self.manager_a)

    def test_a_provider_reviewing_the_proposal_reads_all_of_it(self):
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.manager_a,
            state=models.Review.States.IN_REVIEW,
        )
        self.assertAdmitted(self.manager_a)
