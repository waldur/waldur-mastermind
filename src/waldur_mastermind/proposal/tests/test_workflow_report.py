import io

import pypdf
from django.core.cache import cache
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import workflow_report
from waldur_mastermind.proposal.tests import factories, fixtures


class WorkflowReportTest(test.APITestCase):
    def setUp(self):
        # The endpoint is throttled per user; earlier tests' requests must
        # not count against these.
        cache.clear()
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call, "workflow-report")

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    def text(self, response):
        reader = pypdf.PdfReader(io.BytesIO(response.content))
        return "\n".join(page.extract_text() for page in reader.pages)

    def test_call_team_staff_and_support_download_it(self):
        for user in (
            self.fixture.call_manager,
            self.fixture.call_organizer_user,
            self.fixture.staff,
            structure_factories.UserFactory(is_support=True),
        ):
            with self.subTest(user=user):
                response = self.get(user)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertTrue(response.content.startswith(b"%PDF"))

    def test_panel_reviewers_and_applicants_do_not(self):
        for user in (
            self.fixture.panel_member,
            self.fixture.reviewer_1,
            self.fixture.proposal_creator,
            structure_factories.UserFactory(),
        ):
            with self.subTest(user=user):
                self.assertEqual(self.get(user).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_report_holds_every_section(self):
        text = self.text(self.get(self.fixture.staff))
        for heading in (
            "Workflow check",
            "Steps",
            "Roles",
            "Notifications",
            "Scenarios",
        ):
            self.assertIn(heading, text)
        self.assertIn(self.call.name, text)
        step = self.call.workflow_steps.order_by("order").first()
        self.assertIn(step.name, text)

    def test_findings_come_errors_first_and_name_their_step(self):
        data = workflow_report.get_workflow_report_data(self.call)
        levels = [finding["level"] for finding in data["findings"]]
        order = {"error": 0, "warning": 1, "info": 2}
        self.assertEqual(levels, sorted(levels, key=order.__getitem__))
        for finding in data["findings"]:
            self.assertTrue(finding["where"])

    def test_the_report_names_nobody(self):
        manager = self.fixture.call_manager
        response = self.get(self.fixture.staff)
        text = self.text(response)
        for value in (manager.username, manager.email, manager.full_name):
            if value:
                self.assertNotIn(value, text)
