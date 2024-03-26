from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import move_project
from waldur_mastermind.marketplace.tests import fixtures
from waldur_openstack.openstack.tests import factories as openstack_factories


class ProjectMoveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.project = self.fixture.offering.project
        self.old_customer = self.project.customer
        self.new_customer = structure_factories.CustomerFactory()

    def change_customer(self):
        move_project(self.project, self.new_customer)
        self.project.refresh_from_db()

    def test_change_customer_if_offering_scope_is_customer_open_stack(self):
        customer_open_stack = openstack_factories.CustomerOpenStackFactory(
            customer=self.old_customer
        )
        self.offering.scope = customer_open_stack
        self.offering.save()

        self.change_customer()
        self.assertEqual(self.new_customer, self.project.customer)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.new_customer)

        customer_open_stack.refresh_from_db()
        self.assertEqual(customer_open_stack.customer, self.new_customer)

    def test_change_customer_if_offering_scope_is_tenant(self):
        tenant = openstack_factories.TenantFactory(project=self.project)
        self.offering.scope = tenant
        self.offering.save()

        self.change_customer()
        self.assertEqual(self.new_customer, self.project.customer)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.new_customer)

        tenant.refresh_from_db()
        self.assertEqual(tenant.customer, self.new_customer)
