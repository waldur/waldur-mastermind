from ddt import data, ddt
from django import template
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.templatetags.waldur_marketplace import plan_details

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
        return self.client.patch(self.url, {'name': 'New plan'})


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


class PlanRenderTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component_fix = factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.offering_component_usage = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            type='ram',
        )
        self.offering_component_one = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.ONE_TIME,
            type='one',
        )
        self.offering_component_one_switch = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH,
            type='switch',
        )
        self.component_fix = factories.PlanComponentFactory(
            component=self.offering_component_fix, plan=self.plan
        )
        self.component_usage = factories.PlanComponentFactory(
            component=self.offering_component_usage, plan=self.plan
        )
        self.component_one = factories.PlanComponentFactory(
            component=self.offering_component_one, plan=self.plan
        )
        self.component_one_switch = factories.PlanComponentFactory(
            component=self.offering_component_one_switch, plan=self.plan
        )

    def test_plan_render(self):
        rendered_plan = plan_details(self.plan)

        context = {
            'plan': self.plan,
            'components': [
                {
                    'name': self.component_fix.component.name,
                    'amount': self.component_fix.amount,
                    'price': self.component_fix.price,
                },
                {
                    'name': self.component_one.component.name,
                    'amount': _('one-time fee'),
                    'price': self.component_one.price,
                },
                {
                    'name': self.component_one_switch.component.name,
                    'amount': _('one-time on plan switch'),
                    'price': self.component_one_switch.price,
                },
            ],
        }
        plan_template = get_template(
            'marketplace/marketplace_plan_template.txt'
        ).template
        rendered_plan_expected = plan_template.render(
            template.Context(context, autoescape=False)
        )

        self.assertEqual(rendered_plan, rendered_plan_expected)


@ddt
class PlanDivisionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(self.plan, action='update_divisions')
        self.delete_url = factories.PlanFactory.get_url(
            self.plan, action='delete_divisions'
        )
        self.division = structure_factories.DivisionFactory()
        self.division_url = structure_factories.DivisionFactory.get_url(self.division)

    @data('staff', 'owner')
    def test_user_can_update_divisions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {'divisions': [self.division_url]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.plan.divisions.count(), 1)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_user_cannot_update_divisions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {'divisions': [self.division_url]})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner')
    def test_user_can_delete_divisions(self, user):
        self.plan.divisions.add(self.division)
        self.customer.division = self.division
        self.customer.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        self.plan.refresh_from_db()
        self.assertEqual(self.offering.divisions.count(), 0)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_user_cannot_delete_divisions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_get_all_plans(self):
        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        url = factories.PlanFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

        self.plan.divisions.add(self.division)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)

    def test_owner_can_get_his_plans(self):
        self.client.force_authenticate(getattr(self.fixture, 'owner'))
        url = factories.PlanFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

        self.plan.divisions.add(self.division)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 0)

        self.customer.division = self.division
        self.customer.save()
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_user_cannot_get_not_his_plans(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.PlanFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

        self.plan.divisions.add(self.division)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 0)

        self.customer.division = self.division
        self.customer.save()
        response = self.client.get(url)
        self.assertEqual(len(response.data), 0)

    def test_filter_offerings_plans_by_divisions(self):
        new_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.staff)
        self.offering.divisions.add(self.division)
        url = factories.OfferingFactory.get_list_url()

        response = self.client.get(
            url, {'allowed_customer_uuid': new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        new_customer.division = self.division
        new_customer.save()
        response = self.client.get(
            url, {'allowed_customer_uuid': new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(
            url, {'allowed_customer_uuid': new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

        other_division = structure_factories.DivisionFactory()
        second_other_division = structure_factories.DivisionFactory()
        self.plan.divisions.add(other_division)
        self.plan.divisions.add(second_other_division)
        response = self.client.get(
            url, {'allowed_customer_uuid': new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 0)
