"""Previewing a real proposal as a role of its call: the real read
endpoints answering a synthetic person, nothing kept."""

from rest_framework import status, test

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole
from waldur_mastermind.proposal import dry_run, models, workflow_service
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class ProposalPreviewTest(test.APITestCase):
    def setUp(self):
        # The run's database timeouts are counted from the test's own
        # transaction on recent Postgres; a preview is one short read.
        self.patch_timeout = dry_run.TRANSACTION_TIMEOUT_SINCE
        dry_run.TRANSACTION_TIMEOUT_SINCE = 10**9
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal_submitted
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        workflow_service.create_step_instances(self.proposal)
        workflow_service.activate_first_step(self.proposal)
        self.roles_url = factories.ProposalFactory.get_url(
            self.proposal, "preview-roles"
        )
        self.url = factories.ProposalFactory.get_url(self.proposal, "preview-as")

    def tearDown(self):
        dry_run.TRANSACTION_TIMEOUT_SINCE = self.patch_timeout

    def post(self, user, data):
        self.client.force_authenticate(user)
        return self.client.post(self.url, data, format="json")

    def test_the_call_team_lists_the_roles(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.roles_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        roles = {row["role"] for row in response.data}
        self.assertIn("applicant", roles)
        self.assertTrue(all(row["label"] for row in response.data))

    def test_a_preview_reads_as_a_synthetic_person_and_keeps_nothing(self):
        call_manager = self.fixture.call_manager
        users, grants = User.objects.count(), UserRole.objects.count()
        response = self.post(call_manager, {"role": "applicant"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["viewer"]["username"].startswith("dry-run-"))
        proposal = response.data["responses"]["proposal"]
        self.assertEqual(proposal["status"], status.HTTP_200_OK)
        self.assertEqual(proposal["data"]["uuid"], self.proposal.uuid.hex)
        self.assertEqual(User.objects.count(), users)
        self.assertEqual(UserRole.objects.count(), grants)

    def test_a_preview_reads_the_whole_page(self):
        response = self.post(self.fixture.call_manager, {"role": "applicant"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        responses = response.data["responses"]
        # The proposal's own content, as the page lays it out, not only the
        # workflow's parts: the call's settings, the team, the compliance.
        for key in ("call", "team", "compliance", "my_role_assignments"):
            self.assertIn(key, responses)
        self.assertEqual(responses["team"]["status"], status.HTTP_200_OK)
        self.assertEqual(
            responses["call"]["data"]["uuid"], self.proposal.round.call.uuid.hex
        )

    def test_never_as_a_real_reviewer(self):
        review = factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.fixture.reviewer_1
        )
        roles = {row["role"] for row in dry_run.preview_roles(self.proposal)}
        if "reviewer" not in roles:
            self.skipTest("The fixture's call has no reviewer role.")
        reviews = models.Review.objects.filter(proposal=self.proposal).count()
        response = self.post(self.fixture.staff, {"role": "reviewer"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotEqual(response.data["viewer"]["uuid"], review.reviewer.uuid.hex)
        self.assertEqual(
            models.Review.objects.filter(proposal=self.proposal).count(), reviews
        )

    def test_a_role_off_the_call_is_refused(self):
        response = self.post(self.fixture.call_manager, {"role": "nobody"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_the_call_team_previews(self):
        for user in (
            self.fixture.proposal_submitted_creator,
            self.fixture.reviewer_1,
        ):
            with self.subTest(user=user.username):
                self.assertEqual(
                    self.post(user, {"role": "applicant"}).status_code,
                    status.HTTP_404_NOT_FOUND,
                )
