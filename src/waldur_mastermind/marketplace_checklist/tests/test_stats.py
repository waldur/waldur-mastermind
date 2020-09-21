from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common import utils as common_utils

from .. import models, views
from . import factories


class ChecklistStatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.customer = self.fixture.customer
        self.project_1 = self.fixture.project
        self.project_2 = structure_factories.ProjectFactory(customer=self.customer)
        self.checklist = factories.ChecklistFactory()
        self.question1 = factories.QuestionFactory(checklist=self.checklist)
        self.question2 = factories.QuestionFactory(checklist=self.checklist)
        models.Answer.objects.create(
            user=self.fixture.manager, question=self.question1, value=True
        )
        models.Answer.objects.create(
            user=self.fixture.manager, question=self.question2, value=True
        )

    def get_customer_stats(self):
        url = reverse(
            'marketplace-checklists-customer-stats',
            kwargs={
                'customer_uuid': self.customer.uuid.hex,
                'checklist_uuid': self.checklist.uuid.hex,
            },
        )
        self.client.force_authenticate(self.fixture.staff)
        return self.client.get(url).data

    def test_customer_stats(self):
        stats = self.get_customer_stats()
        self.assertEqual(
            stats,
            [
                {
                    'name': self.project_1.name,
                    'uuid': self.project_1.uuid.hex,
                    'score': 100.0,
                },
                {
                    'name': self.project_2.name,
                    'uuid': self.project_2.uuid.hex,
                    'score': 0.0,
                },
            ],
        )


class AnswerTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.UserFixture()
        self.question = factories.QuestionFactory()
        self.view = views.AnswersSubmitView.as_view({'post': 'create'})

    def test_create_answer(self):
        response = common_utils.create_request(
            self.view,
            self.fixture.staff,
            post_data=[{'question_uuid': self.question.uuid.hex, 'value': True}],
            checklist_uuid=self.question.checklist.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Answer.objects.filter(
                question=self.question, user=self.fixture.staff
            ).exists()
        )
