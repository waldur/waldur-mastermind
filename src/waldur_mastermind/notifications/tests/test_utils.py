from rest_framework import test

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

    def test_offering_and_customer_are_specified(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        users = get_users_for_query(
            {
                'customers': [self.fixture.customer],
                'offerings': [self.offering],
            }
        )
        self.assertIn(owner, users)
        self.assertIn(manager, users)

    def test_all_users(self):
        owner = self.fixture.owner
        manager = self.fixture.manager
        users = get_users_for_query(
            {
                'all_users': True,
            }
        )
        self.assertIn(owner, users)
        self.assertIn(manager, users)
