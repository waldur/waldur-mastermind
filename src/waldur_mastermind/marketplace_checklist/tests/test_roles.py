from django.urls import reverse
from rest_framework import test

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace_checklist.tests import factories


class ChecklistRolesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.project = self.fixture.customer
        self.checklist1 = factories.ChecklistFactory()
        self.checklist2 = factories.ChecklistFactory()
        self.url = reverse("marketplace-checklist-list")

    def test_filter_by_project_roles(self):
        self.checklist1.roles.add(ProjectRole.MANAGER)
        self.checklist2.roles.add(ProjectRole.ADMIN)

        self.client.force_authenticate(self.fixture.manager)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["uuid"], self.checklist1.uuid.hex)

        self.client.force_authenticate(self.fixture.admin)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["uuid"], self.checklist2.uuid.hex)

    def test_filter_by_customer_roles(self):
        self.checklist1.roles.add(CustomerRole.OWNER)
        self.checklist2.roles.add(CustomerRole.SUPPORT)

        self.client.force_authenticate(self.fixture.owner)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["uuid"], self.checklist1.uuid.hex)

        self.client.force_authenticate(self.fixture.customer_support)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["uuid"], self.checklist2.uuid.hex)

    def test_checklist_without_roles_available_to_any_authorized_user(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self.client.get(self.url).data
        self.assertEqual(len(data), 2)
