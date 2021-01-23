from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace_checklist import models
from waldur_mastermind.marketplace_checklist.tests import factories


class CustomerChecklistTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.project = self.fixture.customer
        self.checklist1 = factories.ChecklistFactory()
        self.checklist2 = factories.ChecklistFactory()
        self.url = reverse('marketplace-checklist-list')

    def test_filter_by_project_roles(self):
        models.ChecklistProjectRole.objects.create(
            checklist=self.checklist1, role=models.ProjectRole.MANAGER
        )
        models.ChecklistProjectRole.objects.create(
            checklist=self.checklist2, role=models.ProjectRole.ADMINISTRATOR
        )

        self.client.force_authenticate(self.fixture.manager)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['uuid'], self.checklist1.uuid.hex)

        self.client.force_authenticate(self.fixture.admin)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['uuid'], self.checklist2.uuid.hex)

    def test_filter_by_customer_roles(self):
        models.ChecklistCustomerRole.objects.create(
            checklist=self.checklist1, role=models.CustomerRole.OWNER
        )
        models.ChecklistCustomerRole.objects.create(
            checklist=self.checklist2, role=models.CustomerRole.SUPPORT
        )

        self.client.force_authenticate(self.fixture.owner)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['uuid'], self.checklist1.uuid.hex)

        self.client.force_authenticate(self.fixture.customer_support)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['uuid'], self.checklist2.uuid.hex)

    def test_checklist_without_roles_available_to_any_authorized_user(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 2)
