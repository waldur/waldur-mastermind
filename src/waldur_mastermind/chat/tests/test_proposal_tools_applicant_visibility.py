"""The proposal tools show a reviewer only the applicant details the call's
applicant visibility config exposes -- the same set the proposal API gives
that reviewer -- and everyone else the applicant in full."""

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.proposals_researcher.list_proposals import (
    ListProposalsTool,
)
from waldur_mastermind.chat.tools.proposals_researcher.proposal_overview import (
    ProposalOverviewTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.review_assistant import (
    ReviewAssistantTool,
)
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures

APPLICANT_NAME = "Applicant Fullname"
APPLICANT_ORGANIZATION = "Applicant Organisation"


class ApplicantVisibilityInProposalToolsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal_submitted
        self.call = self.proposal.round.call
        applicant = self.proposal.created_by
        applicant.full_name = APPLICANT_NAME
        applicant.organization = APPLICANT_ORGANIZATION
        applicant.save()
        self.reviewer = self.fixture.reviewer_1
        factories.ReviewFactory(proposal=self.proposal, reviewer=self.reviewer)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def conceal_identity(self):
        models.CallApplicantVisibilityConfig.objects.update_or_create(
            call=self.call,
            defaults={
                "expose_full_name": False,
                "expose_username": False,
                "expose_email": False,
            },
        )

    def review_assistant(self, user):
        result = ReviewAssistantTool().execute(user, {"uuid": self.proposal.uuid.hex})
        self.assertEqual(result["type"], "success", result)
        return result["data"]["proposal"], result

    def proposal_overview(self, user):
        result = ProposalOverviewTool().execute(user, {"uuid": self.proposal.uuid.hex})
        self.assertEqual(result["type"], "success", result)
        return result["data"], result

    def listed_proposal(self, user):
        result = ListProposalsTool().execute(user, {"call_uuid": self.call.uuid.hex})
        self.assertEqual(result["type"], "success", result)
        rows = [
            p for p in result["data"]["proposals"] if p["slug"] == self.proposal.slug
        ]
        self.assertEqual(len(rows), 1, result)
        return rows[0], result

    # Default config: name, username and email exposed; organization not.

    def test_review_assistant_hides_organization_by_default(self):
        data, result = self.review_assistant(self.reviewer)
        self.assertEqual(data["created_by"], APPLICANT_NAME)
        self.assertNotIn("organization", data)
        self.assertNotIn(APPLICANT_ORGANIZATION, repr(result))

    def test_proposal_overview_hides_organization_by_default(self):
        data, result = self.proposal_overview(self.reviewer)
        self.assertEqual(data["created_by"], APPLICANT_NAME)
        self.assertNotIn("created_by_org", data)
        self.assertNotIn(APPLICANT_ORGANIZATION, repr(result))

    # A call that exposes no identity attribute.

    def test_review_assistant_hides_concealed_identity(self):
        self.conceal_identity()
        data, result = self.review_assistant(self.reviewer)
        self.assertNotIn("created_by", data)
        self.assertNotIn("organization", data)
        self.assertNotIn(APPLICANT_NAME, repr(result))
        self.assertNotIn(APPLICANT_ORGANIZATION, repr(result))

    def test_proposal_overview_hides_concealed_identity(self):
        self.conceal_identity()
        data, result = self.proposal_overview(self.reviewer)
        self.assertNotIn("created_by", data)
        self.assertNotIn("created_by_org", data)
        self.assertNotIn(APPLICANT_NAME, repr(result))
        self.assertNotIn(APPLICANT_ORGANIZATION, repr(result))

    def test_list_proposals_hides_concealed_identity(self):
        self.conceal_identity()
        row, result = self.listed_proposal(self.reviewer)
        self.assertNotIn("created_by", row)
        self.assertNotIn(APPLICANT_NAME, repr(result))

    def test_list_proposals_shows_exposed_name(self):
        row, _ = self.listed_proposal(self.reviewer)
        self.assertEqual(row["created_by"], APPLICANT_NAME)

    # Viewers who are not solely evaluators keep the applicant in full.

    def test_staff_sees_applicant_in_full_despite_concealing_config(self):
        self.conceal_identity()
        data, _ = self.review_assistant(self.staff)
        self.assertEqual(data["created_by"], APPLICANT_NAME)
        self.assertEqual(data["organization"], APPLICANT_ORGANIZATION)
        data, _ = self.proposal_overview(self.staff)
        self.assertEqual(data["created_by"], APPLICANT_NAME)
        self.assertEqual(data["created_by_org"], APPLICANT_ORGANIZATION)
        row, _ = self.listed_proposal(self.staff)
        self.assertEqual(row["created_by"], APPLICANT_NAME)
