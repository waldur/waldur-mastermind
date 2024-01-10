from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace_checklist.tests import factories


class CustomerChecklistTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.customer = self.fixture.customer
        self.checklist1 = factories.ChecklistFactory()
        self.checklist2 = factories.ChecklistFactory()
        self.url = reverse(
            "marketplace-checklists-customer",
            kwargs={"customer_uuid": self.customer.uuid.hex},
        )

    def get_customer_checklists(self):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.get(self.url).data

    def test_empty(self):
        self.assertEqual(self.get_customer_checklists(), [])

    def test_non_empty(self):
        self.checklist1.customers.add(self.customer)
        self.assertEqual(
            self.get_customer_checklists(),
            [self.checklist1.uuid],
        )

    def test_update_to_empty(self):
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.url, [])
        self.assertEqual(self.get_customer_checklists(), [])

    def test_update_to_non_empty(self):
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.url, [self.checklist2.uuid])
        self.assertEqual(self.get_customer_checklists(), [self.checklist2.uuid])
