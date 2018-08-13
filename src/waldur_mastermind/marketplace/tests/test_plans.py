from __future__ import unicode_literals

from ddt import data, ddt
from rest_framework import status

from waldur_core.core.tests.utils import PostgreSQLTest
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models

from . import factories


@ddt
class PlanGetTest(PostgreSQLTest):

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
class PlanCreateTest(PostgreSQLTest):

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
            'unit_price': 100
        }
        return self.client.post(url, payload)


@ddt
class PlanUpdateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_plan(self, user):
        response, plan = self.update_plan(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(plan.name, 'new_plan')
        self.assertTrue(models.Plan.objects.filter(name='new_plan').exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_plan(self, user):
        response, plan = self.update_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_plan(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        self.offering = factories.OfferingFactory(customer=self.customer)
        plan = factories.PlanFactory(offering=self.offering)
        url = factories.PlanFactory.get_url(plan)

        response = self.client.patch(url, {
            'name': 'new_plan'
        })
        plan.refresh_from_db()

        return response, plan


@ddt
class PlanDeleteTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(self.plan)

    @data('staff', 'owner')
    def test_authorized_user_can_delete_plan(self, user):
        response = self.delete_plan(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.Plan.objects.filter(offering=self.offering).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_plan(self, user):
        response = self.delete_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Plan.objects.filter(offering=self.offering).exists())

    def delete_plan(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        return response
