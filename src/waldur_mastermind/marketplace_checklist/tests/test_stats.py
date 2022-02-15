from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace_checklist import models, views
from waldur_mastermind.marketplace_checklist.tests import factories


class ChecklistStatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.customer = self.fixture.customer
        self.project_1 = self.fixture.project
        self.project_1.name = 'Project 1'
        self.project_1.save()
        self.project_2 = structure_factories.ProjectFactory(customer=self.customer)
        self.project_2.name = 'Project 2'
        self.project_2.save()
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

    def get_user_stats(self, user):
        url = reverse(
            'marketplace-checklist-user-stats',
            kwargs={
                'user_uuid': user.uuid.hex,
            },
        )
        self.client.force_authenticate(self.fixture.staff)
        return self.client.get(url).data['score']

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

    def test_user_stats_when_all_answers_are_correct(self):
        stats = self.get_user_stats(self.fixture.manager)
        self.assertEqual(stats, 100)

    def test_user_stats_when_there_are_no_correct_answers(self):
        stats = self.get_user_stats(self.fixture.admin)
        self.assertEqual(stats, 0)


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

    def test_create_answer_on_behalf_by_staff_is_allowed(self):
        response = common_utils.create_request(
            self.view,
            self.fixture.staff,
            post_data=[{'question_uuid': self.question.uuid.hex, 'value': True}],
            query_params={'on_behalf_user_uuid': self.fixture.user.uuid.hex},
            checklist_uuid=self.question.checklist.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Answer.objects.filter(
                question=self.question, user=self.fixture.user
            ).exists()
        )

    def test_create_answer_on_behalf_by_non_staff_is_ignored(self):
        response = common_utils.create_request(
            self.view,
            self.fixture.global_support,
            post_data=[{'question_uuid': self.question.uuid.hex, 'value': True}],
            query_params={'on_behalf_user_uuid': self.fixture.user.uuid.hex},
            checklist_uuid=self.question.checklist.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(
            models.Answer.objects.filter(
                question=self.question, user=self.fixture.user
            ).exists()
        )

    def test_create_answer_on_behalf_raises_error_when_uuid_has_invalid_format(self):
        response = common_utils.create_request(
            self.view,
            self.fixture.staff,
            post_data=[{'question_uuid': self.question.uuid.hex, 'value': True}],
            query_params={'on_behalf_user_uuid': 'INVALID'},
            checklist_uuid=self.question.checklist.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_answer_on_behalf_raises_error_when_uuid_is_invalid(self):
        response = common_utils.create_request(
            self.view,
            self.fixture.staff,
            post_data=[{'question_uuid': self.question.uuid.hex, 'value': True}],
            query_params={
                'on_behalf_user_uuid': 'bb223745-1111-1111-1111-c3ae54678d38'
            },
            checklist_uuid=self.question.checklist.uuid.hex,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
