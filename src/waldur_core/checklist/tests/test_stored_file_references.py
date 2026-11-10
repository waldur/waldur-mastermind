"""An answer may send back only the stored files its own checklist holds.

An already-stored file entry (an id, no content) is kept as it is sent, and
whoever wrote an answer naming a file may download it. Naming a file stored
for another checklist would hand its author that file.
"""

import base64
import os

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.enums import ChecklistTypes, QuestionTypes
from waldur_core.checklist.tests import factories
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


def _pdf_base64():
    path = os.path.join(os.path.dirname(__file__), "minimal_word_file.pdf")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


class StoredFileReferenceTest(test.APITestCase):
    def setUp(self):
        self.checklist = factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        )
        self.question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=QuestionTypes.MULTIPLE_FILES,
            allowed_file_types=[".pdf"],
            allowed_mime_types=["application/pdf"],
            max_file_size_mb=10,
        )
        self.victim = self._project_fixture()
        self.attacker = self._project_fixture()

        answer = models.Answer.objects.create(
            user=self.victim.owner,
            question=self.question,
            completion=self._completion(self.victim),
            answer_data=[{"name": "budget.pdf", "content": _pdf_base64()}],
        )
        self.stored_file = answer.answer_data[0]

    def _project_fixture(self):
        fixture = structure_fixtures.ProjectFixture()
        fixture.customer.project_metadata_checklist = self.checklist
        fixture.customer.save()
        return fixture

    def _completion(self, fixture):
        return models.ChecklistCompletion.objects.get(
            checklist=self.checklist,
            scope_content_type=ContentType.objects.get_for_model(fixture.project),
            scope_object_id=fixture.project.id,
        )

    def _submit(self, fixture, user, answer_data):
        self.client.force_authenticate(user)
        url = structure_factories.ProjectFactory.get_url(
            fixture.project, action="submit_answers"
        )
        return self.client.post(
            url,
            [{"question_uuid": str(self.question.uuid), "answer_data": answer_data}],
            format="json",
        )

    def _download(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            reverse("media", kwargs={"uuid": self.stored_file["stored_file_id"]})
        )

    def test_a_file_stored_for_another_checklist_is_refused(self):
        response = self._submit(
            self.attacker, self.attacker.owner, [dict(self.stored_file)]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            self._completion(self.attacker).answers.filter(user=self.attacker.owner)
        )
        self.assertEqual(
            self._download(self.attacker.owner).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_one_foreign_file_among_new_uploads_refuses_the_answer(self):
        response = self._submit(
            self.attacker,
            self.attacker.owner,
            [
                {"name": "mine.pdf", "content": _pdf_base64()},
                dict(self.stored_file),
            ],
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_author_can_send_back_the_files_it_holds(self):
        response = self._submit(
            self.victim,
            self.victim.owner,
            [dict(self.stored_file), {"name": "more.pdf", "content": _pdf_base64()}],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        answer = self._completion(self.victim).answers.get(user=self.victim.owner)
        self.assertEqual(len(answer.answer_data), 2)
        self.assertEqual(answer.answer_data[0], self.stored_file)

    def test_another_member_can_send_back_a_file_of_the_same_checklist(self):
        co_owner = structure_factories.UserFactory()
        self.victim.customer.add_user(co_owner, CustomerRole.OWNER)

        response = self._submit(self.victim, co_owner, [dict(self.stored_file)])

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_file_sent_with_its_content_is_stored_afresh(self):
        forged = dict(self.stored_file, content=_pdf_base64())

        response = self._submit(self.attacker, self.attacker.owner, [forged])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        answer = self._completion(self.attacker).answers.get(user=self.attacker.owner)
        self.assertNotEqual(
            answer.answer_data[0]["stored_file_id"],
            self.stored_file["stored_file_id"],
        )
        self.assertEqual(
            self._download(self.attacker.owner).status_code,
            status.HTTP_404_NOT_FOUND,
        )
