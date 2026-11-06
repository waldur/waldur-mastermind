"""The files attached to a proposal's compliance answers open for whoever
reads those answers -- the applicant team, the call's managers and
organisers -- and for nobody else."""

import base64
from pathlib import Path

from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.tests import fixtures

PDF = (
    Path(__file__).resolve().parents[3]
    / "waldur_core/checklist/tests/minimal_word_file.pdf"
).read_bytes()


class ComplianceFileAccessTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        CallRole.MANAGER.add_permission(PermissionEnum.LIST_PROPOSALS)
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.PROPOSAL_COMPLIANCE
        )
        question = checklist_factories.QuestionFactory(
            checklist=checklist,
            question_type=checklist_enums.QuestionTypes.FILE,
            allowed_file_types=[".pdf"],
        )
        completion = checklist_models.ChecklistCompletion.objects.create(
            checklist=checklist, scope=self.proposal
        )
        self.author = structure_factories.UserFactory()
        self.proposal.add_user(self.author, ProposalRole.MANAGER)
        answer = checklist_models.Answer.objects.create(
            user=self.author,
            question=question,
            completion=completion,
            answer_data={
                "name": "ethics.pdf",
                "content": base64.b64encode(PDF).decode(),
            },
        )
        self.url = reverse(
            "media", kwargs={"uuid": answer.answer_data["stored_file_id"]}
        )

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    def test_those_who_read_the_answers_open_the_files(self):
        team_member = structure_factories.UserFactory()
        self.proposal.add_user(team_member, ProposalRole.MEMBER)
        manager = structure_factories.UserFactory()
        self.fixture.call.add_user(manager, CallRole.MANAGER)
        for user in (
            self.author,
            team_member,
            manager,
            self.fixture.call_organizer_user,
        ):
            response = self.get(user)
            self.assertEqual(response.status_code, status.HTTP_200_OK, user)
            self.assertEqual(response.content, PDF)

    def test_nobody_else_does(self):
        self.assertEqual(
            self.get(structure_factories.UserFactory()).status_code,
            status.HTTP_404_NOT_FOUND,
        )
