from rest_framework import test

from waldur_core.structure.models import CustomerRole, ProjectRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from ..utils import get_users_for_query


class UsersFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = marketplace_factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )

    def test_offering_and_customer_roles_are_specified(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        users = get_users_for_query(
            {
                'customers': [self.fixture.customer],
                'customer_roles': [CustomerRole.OWNER],
                'offerings': [self.offering],
            }
        )
        self.assertIn(owner, users)
        self.assertNotIn(manager, users)

    def test_offering_and_project_roles_are_specified(self):
        manager = self.fixture.manager
        admin = self.fixture.admin
        users = get_users_for_query(
            {
                'projects': [self.fixture.project],
                'project_roles': [ProjectRole.MANAGER],
                'offerings': [self.offering],
            }
        )
        self.assertIn(manager, users)
        self.assertNotIn(admin, users)

    def test_project_is_specified_explicitly(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        admin = self.fixture.admin
        users = get_users_for_query(
            {
                'projects': [self.fixture.project],
            }
        )
        self.assertIn(manager, users)
        self.assertIn(admin, users)
        self.assertNotIn(owner, users)

    def test_customer_is_specified_explicitly(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        admin = self.fixture.admin
        users = get_users_for_query(
            {
                'customers': [self.fixture.customer],
                'customer_roles': [CustomerRole.OWNER],
            }
        )
        self.assertNotIn(manager, users)
        self.assertNotIn(admin, users)
        self.assertIn(owner, users)

    def test_if_customer_is_not_defined_related_resources_are_used(self):
        owner = self.fixture.owner
        users = get_users_for_query(
            {
                'customer_roles': [CustomerRole.OWNER],
                'offerings': [self.offering],
            }
        )
        self.assertIn(owner, users)

    def test_if_project_is_not_defined_related_resources_are_used(self):
        manager = self.fixture.manager
        users = get_users_for_query(
            {
                'project_roles': [ProjectRole.MANAGER],
                'offerings': [self.offering],
            }
        )
        self.assertIn(manager, users)
