from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import models
from . import factories


class ChecklistStatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.customer = self.fixture.customer
        self.project1 = structure_factories.ProjectFactory(customer=self.customer)
        self.project2 = structure_factories.ProjectFactory(customer=self.customer)
        self.checklist = factories.ChecklistFactory()
        self.question1 = factories.QuestionFactory(checklist=self.checklist)
        self.question2 = factories.QuestionFactory(checklist=self.checklist)
        models.Answer.objects.create(
            project=self.project1, question=self.question1, value=True
        )
        models.Answer.objects.create(
            project=self.project1, question=self.question2, value=True
        )
        models.Answer.objects.create(
            project=self.project2, question=self.question1, value=False
        )
        models.Answer.objects.create(
            project=self.project2, question=self.question2, value=False
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
                    'name': self.project1.name,
                    'uuid': self.project1.uuid.hex,
                    'score': 100.0,
                },
                {
                    'name': self.project2.name,
                    'uuid': self.project2.uuid.hex,
                    'score': 0.0,
                },
            ],
        )
