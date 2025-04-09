from ddt import ddt
from rest_framework import status, test

from waldur_core.permissions.utils import get_permissions
from waldur_core.structure import views
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.utils import move_project
from waldur_mastermind.marketplace.tests import fixtures
from waldur_openstack.tests import factories as openstack_factories


@ddt
class ProjectMoveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.project = self.fixture.offering.project
        self.old_customer = self.project.customer
        self.new_customer = structure_factories.CustomerFactory()

        self.old_permissions = set(get_permissions(self.project))
        self.new_permissions = set(get_permissions(self.new_customer))

        self.fixture.staff = structure_factories.UserFactory(is_staff=True)
        self.fixture.owner = structure_factories.UserFactory()

        self.view = views.ProjectViewSet.as_view({"post": "move_project"})

    def change_customer(self, preserve_permissions):
        move_project(self.project, self.new_customer, preserve_permissions)
        self.project.refresh_from_db()

    def test_change_customer_if_offering_scope_is_customer_open_stack(self):
        customer_open_stack = openstack_factories.CustomerOpenStackFactory(
            customer=self.old_customer
        )
        self.offering.scope = customer_open_stack
        self.offering.save()

        self.change_customer(preserve_permissions=False)
        self.assertEqual(self.new_customer, self.project.customer)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.new_customer)

        customer_open_stack.refresh_from_db()
        self.assertEqual(customer_open_stack.customer, self.new_customer)

    def test_change_customer_if_offering_scope_is_tenant(self):
        tenant = openstack_factories.TenantFactory(project=self.project)
        self.offering.scope = tenant
        self.offering.save()

        self.change_customer(preserve_permissions=False)
        self.assertEqual(self.new_customer, self.project.customer)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.new_customer)

        tenant.refresh_from_db()
        self.assertEqual(tenant.customer, self.new_customer)

    def create_payload(self):
        return {
            "name": self.project.name,
            "preserve_permissions": True,
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
        }

    def test_change_customer_if_permissions_preserved_rest(self):
        """
        Test that permissions are preserved when moving a project to a new customer using REST API.
        """
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            structure_factories.ProjectFactory.get_url(
                self.project, action="move_project"
            ),
            {
                "customer": structure_factories.CustomerFactory.get_url(
                    self.new_customer
                ),
                "preserve_permissions": True,
            },
            format="json",
        )
        # Check that the response status code is 200
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected status code 200, got {response.status_code}",
        )
        # Refresh the project from the database
        self.project.refresh_from_db()
        # Check that the new customer is set
        self.assertEqual(
            self.new_customer,
            self.project.customer,
            f"Expected customer {self.new_customer} to be set, got {self.project.customer}",
        )
        # Refresh the offering from the database
        self.offering.refresh_from_db()

        self.new_permissions = set(get_permissions(self.project))
        # Check that permissions are preserved
        self.assertSetEqual(
            self.old_permissions,
            self.new_permissions,
            f"Expected permissions {self.old_permissions} to be preserved, got {self.new_permissions}",
        )

    def test_change_customer_if_permissions_preserved(self):
        """
        Test that permissions are preserved when moving a project to a new customer.
        """
        # Get current permissions before move
        old_permissions = set(get_permissions(self.project))

        # Move project with permission preservation
        self.change_customer(preserve_permissions=True)

        # Get permissions after move
        new_permissions = set(get_permissions(self.project))

        # Verify permissions were preserved
        self.assertSetEqual(old_permissions, new_permissions)
