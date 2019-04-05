from __future__ import unicode_literals

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models

from . import factories


@ddt
class PlanGetTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_plans_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.PlanFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_plans_should_be_invisible_to_unauthenticated_users(self):
        url = factories.PlanFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class PlanCreateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)

    @data('staff', 'owner')
    def test_can_create_plan(self, user):
        response = self.create_plan(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Plan.objects.filter(offering=self.offering).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_can_not_create_plan(self, user):
        response = self.create_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_plan(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.PlanFactory.get_list_url()
        payload = {
            'name': 'plan',
            'offering': factories.OfferingFactory.get_url(self.offering),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'unit': UnitPriceMixin.Units.QUANTITY,
        }
        return self.client.post(url, payload)


@ddt
class PlanUpdateTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(self.plan)

    @data('staff', 'owner')
    def test_authorized_user_can_update_plan(self, user):
        response = self.update_plan(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.name, 'New plan')

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_plan(self, user):
        response = self.update_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_it_should_not_be_possible_to_update_plan_for_an_existing_resources(self):
        factories.ResourceFactory(offering=self.offering, plan=self.plan)
        response = self.update_plan('owner')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def update_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {
            'name': 'New plan'
        })


@ddt
class PlanArchiveTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(self.plan, 'archive')

    @data('staff', 'owner')
    def test_authorized_user_can_archive_plan(self, user):
        response = self.archive_plan(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.archived)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_archive_plan(self, user):
        response = self.archive_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.archived)

    def archive_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(self.url)
