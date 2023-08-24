import base64
import json
import os
import tempfile
import uuid
from unittest import mock

import pkg_resources
from ddt import data, ddt
from rest_framework import exceptions as rest_exceptions
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import add_permission
from waldur_core.structure.models import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.management.commands.export_offering import (
    export_offering,
)
from waldur_mastermind.marketplace.management.commands.import_offering import (
    create_offering,
    update_offering,
)
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings
from waldur_mastermind.marketplace_vmware import VIRTUAL_MACHINE_TYPE

from . import fixtures as marketplace_fixtures


@ddt
class OfferingGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True,
            project=self.fixture.project,
            customer=self.fixture.customer,
            state=models.Offering.States.ACTIVE,
        )

    @data('staff', 'global_support', 'owner', 'customer_support', 'admin', 'manager')
    def test_offerings_should_be_visible_to_staff_and_related_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data(
        'user',
    )
    def test_offerings_should_be_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_offerings_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_offerings_should_be_visible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_field_query_param(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {'field': ['divisions']})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(len(response.json()[0].keys()), 1)
        self.assertEqual(list(response.json()[0].keys())[0], 'divisions')


class OfferingExtraFieldsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering_1 = factories.OfferingFactory(shared=True)
        self.offering_2 = factories.OfferingFactory(shared=True)
        self.url = factories.OfferingFactory.get_list_url()
        self.detail_url = factories.OfferingFactory.get_url(self.offering_2)

    def test_total_customers(self):
        self.client.force_authenticate(self.fixture.staff)
        self._check_field_before_set_of_it('total_customers')

        factories.ResourceFactory(
            offering=self.offering_2,
            state=models.Resource.States.OK,
        )

        self._check_field_after_set_of_it('total_customers', 1)

    def test_total_cost_estimated(self):
        self.client.force_authenticate(self.fixture.staff)
        self._check_field_before_set_of_it('total_cost_estimated')

        invoice_item = invoices_factories.InvoiceItemFactory()
        resource = factories.ResourceFactory(
            project=invoice_item.project,
            offering=self.offering_2,
            state=models.Resource.States.OK,
        )
        invoice_item.resource = resource
        invoice_item.unit_price = 10
        invoice_item.quantity = 2
        invoice_item.save()

        self._check_field_after_set_of_it('total_cost_estimated', 20)

    def test_total_cost(self):
        self.client.force_authenticate(self.fixture.staff)
        self._check_field_before_set_of_it('total_cost')

        invoice_item = invoices_factories.InvoiceItemFactory()
        resource = factories.ResourceFactory(
            project=invoice_item.project,
            offering=self.offering_2,
            state=models.Resource.States.OK,
        )
        invoice_item.resource = resource
        invoice_item.unit_price = 10
        invoice_item.quantity = 3
        invoice_item.save()

        last_month = core_utils.get_last_month()
        invoice_item.invoice.year = last_month.year
        invoice_item.invoice.month = last_month.month
        invoice_item.invoice.save()

        self._check_field_after_set_of_it('total_cost', 30)

    def _check_field_before_set_of_it(self, field_name):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertFalse(field_name in response.json()[0].keys())

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(field_name in response.json().keys())

    def _check_field_after_set_of_it(self, field_name, value):
        response = self.client.get(self.url, {'o': '-%s' % field_name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(response.json()[0][field_name], value)
        self.assertEqual(response.json()[1][field_name], 0)

        response = self.client.get(self.url, {'o': field_name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(response.json()[0][field_name], 0)
        self.assertEqual(response.json()[1][field_name], value)


class OfferingPlanInfoTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(shared=True)
        self.url = factories.OfferingFactory.get_url(self.offering)

        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.FIXED,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )

    def test_plan_info(self):
        self.client.force_authenticate(self.fixture.staff)
        self._check_plan_info(models.OfferingComponent.BillingTypes.FIXED, 'fixed')
        self._check_plan_info(
            models.OfferingComponent.BillingTypes.USAGE, 'usage-based'
        )
        self._check_plan_info(
            models.OfferingComponent.BillingTypes.ONE_TIME, 'one-time'
        )
        self._check_plan_info(
            models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH, 'on-plan-switch'
        )
        self._check_plan_info(models.OfferingComponent.BillingTypes.LIMIT, 'limit')

        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.FIXED,
            type='ram',
            name='RAM',
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=offering_component
        )

        self._check_plan_info(
            models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH, 'mixed'
        )

    def test_minimal_price(self):
        self.client.force_authenticate(self.fixture.staff)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.LIMIT
        )
        self.plan_component.price = 10
        self._check_minimal_price(10)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.FIXED
        )
        self.plan_component.price = 100
        self.plan_component.amount = 0
        self._check_minimal_price(100)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.FIXED
        )
        self.plan_component.price = 100
        self.plan_component.amount = 1
        self._check_minimal_price(100)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.ONE_TIME
        )
        self.plan_component.price = 200
        self._check_minimal_price(200)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH
        )
        self.plan_component.price = 300
        self._check_minimal_price(0)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.USAGE
        )
        self.plan_component.price = 500
        self._check_minimal_price(0)

    def _check_minimal_price(self, minimal_price):
        self.offering_component.save()
        self.plan_component.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['plans'][0]['minimal_price'], minimal_price)

    def _check_plan_info(self, billing_type, plan_type):
        self.offering_component.billing_type = billing_type
        self.offering_component.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['plans'][0]['plan_type'], plan_type)


@ddt
class SecretOptionsTests(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer, project=self.fixture.project
        )
        self.url = factories.OfferingFactory.get_url(self.offering)

    @data('staff', 'owner')
    def test_secret_options_are_visible_to_authorized_user(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('secret_options' in response.data)

    @data('customer_support', 'admin', 'manager')
    def test_secret_options_are_not_visible_to_unauthorized_user(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse('secret_options' in response.data)


class OfferingFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        attributes = {
            'cloudDeploymentModel': 'private_cloud',
            'userSupportOption': ['phone'],
        }
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, attributes=attributes, shared=False
        )
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_choice_positive(self):
        response = self.client.get(
            self.url,
            {
                'attributes': json.dumps(
                    {
                        'cloudDeploymentModel': 'private_cloud',
                    }
                )
            },
        )
        self.assertEqual(len(response.data), 1)

    def test_filter_choice_negative(self):
        response = self.client.get(
            self.url,
            {
                'attributes': json.dumps(
                    {
                        'cloudDeploymentModel': 'public_cloud',
                    }
                )
            },
        )
        self.assertEqual(len(response.data), 0)

    def test_filter_list_positive(self):
        """
        If an attribute is a list, we use multiple choices.
        """
        factories.OfferingFactory(
            attributes={
                'userSupportOption': ['phone', 'email', 'fax'],
            }
        )
        factories.OfferingFactory(
            attributes={
                'userSupportOption': ['email'],
            }
        )
        response = self.client.get(
            self.url,
            {
                'attributes': json.dumps(
                    {
                        'userSupportOption': ['fax', 'email'],
                    }
                )
            },
        )
        self.assertEqual(len(response.data), 2)

    def test_shared_offerings_are_not_available_for_all_users(self):
        # Arrange
        factories.OfferingFactory(customer=self.fixture.customer, shared=False)
        self.offering.shared = True
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_not_available_for_users_in_other_customers(self):
        fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offering_is_available_for_users_in_related_project(self):
        fixture = fixtures.ProjectFixture()
        self.offering.project = fixture.project
        self.offering.save()
        self.client.force_authenticate(fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offering_is_not_available_for_users_in_other_project_of_the_same_customer(
        self,
    ):
        fixture = fixtures.ProjectFixture()
        self.offering.project = fixture.project
        self.offering.save()

        other_manager = structure_factories.UserFactory()
        other_project = structure_factories.ProjectFactory(
            customer=fixture.project.customer
        )
        other_project.add_user(other_manager, ProjectRole.MANAGER)

        self.client.force_authenticate(other_manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_not_available_for_users_in_other_projects(self):
        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_available_for_users_in_original_customer(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offerings_are_available_for_staff(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offerings_are_available_for_support(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_filter_offerings_for_service_manager(self):
        # Arrange
        factories.OfferingFactory(customer=self.fixture.customer, shared=False)

        self.offering.shared = True
        self.offering.save()
        self.offering.add_user(self.fixture.user)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, {'service_manager_uuid': self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.offering.uuid.hex)

    def test_filter_limited_shared_offerings_for_customer_uuid_if_divisions_match(
        self,
    ):
        # Arrange
        self.offering.delete()
        offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        url = factories.OfferingFactory.get_list_url()
        division = structure_factories.DivisionFactory()
        offering.divisions.add(division)

        self.fixture.customer.division = division
        self.fixture.customer.save()

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            url, {'allowed_customer_uuid': self.fixture.customer.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], offering.uuid.hex)

    def test_filter_limited_shared_offerings_for_customer_uuid_if_divisions_do_not_match(
        self,
    ):
        # Arrange
        self.offering.delete()
        offering = factories.OfferingFactory(shared=True)
        url = factories.OfferingFactory.get_list_url()
        division = structure_factories.DivisionFactory()
        offering.divisions.add(division)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            url, {'allowed_customer_uuid': self.fixture.customer.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 0)

    def test_filter_keyword(self):
        factories.OfferingFactory(name='name keyword')
        factories.OfferingFactory(description='description Keyword')
        offering = factories.OfferingFactory()
        offering.customer.name = 'name keyword'
        offering.customer.save()
        url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 4)
        response = self.client.get(url, {'keyword': 'keyword'})
        self.assertEqual(len(response.data), 3)


class OfferingPlansFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.shared = True
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()
        self.plan = self.fixture.plan
        self.url = factories.OfferingFactory.get_public_url(self.offering)

    def test_anonymous_user_cannot_get_plans_matched_with_divisions(self):
        url = factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

        division = structure_factories.DivisionFactory()
        self.plan.divisions.add(division)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 0)

    def test_staff_can_get_all_plans(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

        division = structure_factories.DivisionFactory()
        self.plan.divisions.add(division)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

    def test_filtering_plans_by_owner(self):
        self.client.force_authenticate(self.fixture.owner)

        # user can get plans if they are not connected with divisions
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

        division = structure_factories.DivisionFactory()
        self.plan.divisions.add(division)

        # user cannot get plans if they are connected with divisions
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 0)

        self.fixture.customer.division = division
        self.fixture.customer.save()

        # user can get plans if they are connected with the same divisions
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

    def test_filtering_plans_by_admin(self):
        self.client.force_authenticate(self.fixture.admin)

        # user can get plans if they are not connected with divisions
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)

        division = structure_factories.DivisionFactory()
        self.plan.divisions.add(division)

        # user cannot get plans if they are connected with divisions
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 0)

        self.fixture.project.customer.division = division
        self.fixture.project.customer.save()

        # user can get plans if they are connected with the same divisions
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['plans']), 1)


@ddt
class OfferingCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_options_default_value(self):
        response = self.create_offering('staff')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.options, {'options': {}, 'order': []})

    def test_validate_correct_geolocations(self):
        response = self.create_offering(
            'staff', add_payload={'latitude': 123, 'longitude': 345}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_create_offering_with_attributes(self):
        response = self.create_offering('staff', attributes=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(
            offering.attributes,
            {
                'cloudDeploymentModel': 'private_cloud',
                'vendorType': 'reseller',
                'userSupportOptions': ['web_chat', 'phone'],
                'dataProtectionInternal': 'ipsec',
                'dataProtectionExternal': 'tls12',
            },
        )

    def test_dont_create_offering_if_attributes_is_not_valid(self):
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.attribute = factories.AttributeFactory(
            section=self.section, key='userSupportOptions'
        )
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()

        payload = {
            'name': 'offering',
            'native_name': 'native_name',
            'category': factories.CategoryFactory.get_url(category=self.category),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'attributes': json.dumps(
                {
                    'cloudDeploymentModel': 'private_cloud',
                    'vendorType': 'reseller',
                    'userSupportOptions': ['chat', 'phone'],
                    'dataProtectionInternal': 'ipsec',
                    'dataProtectionExternal': 'tls12',
                }
            ),
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_is_not_created_if_attributes_are_not_provided(self):
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()

        payload = {
            'name': 'offering',
            'category': factories.CategoryFactory.get_url(category=self.category),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'attributes': '"String is not allowed, dictionary is expected."',
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch('waldur_azure.backend.AzureClient')
    def test_create_offering_with_shared_service_settings(self, mocked_backend):
        plans_request = {
            'type': 'Azure.VirtualMachine',
            'service_attributes': {
                'tenant_id': uuid.uuid4(),
                'client_id': uuid.uuid4(),
                'client_secret': uuid.uuid4(),
                'subscription_id': uuid.uuid4(),
            },
            'shared': True,
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering = models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertIsNotNone(response.data['scope'])
        self.assertEqual(offering.scope.type, 'Azure')
        self.assertTrue(offering.scope.shared)

    @mock.patch('waldur_azure.backend.AzureClient')
    def test_create_offering_with_private_service_settings(self, mocked_backend):
        plans_request = {
            'type': 'Azure.VirtualMachine',
            'service_attributes': {
                'tenant_id': uuid.uuid4(),
                'client_id': uuid.uuid4(),
                'client_secret': uuid.uuid4(),
                'subscription_id': uuid.uuid4(),
            },
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering = models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertFalse(offering.scope.shared)

    def test_create_offering_with_plans(self):
        plans_request = {
            'plans': [
                {
                    'name': 'Small',
                    'description': 'Basic plan',
                }
            ]
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(len(response.data['plans']), 1)

    def test_specify_max_amount_for_plan(self):
        plans_request = {
            'plans': [
                {
                    'name': 'Small',
                    'description': 'Basic plan',
                    'max_amount': 10,
                }
            ]
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['plans'][0]['max_amount'], 10)

    def test_max_amount_should_be_at_least_one(self):
        plans_request = {
            'plans': [
                {
                    'name': 'Small',
                    'description': 'Basic plan',
                    'max_amount': -1,
                }
            ]
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_with_custom_components(self):
        plans_request = {
            'components': [
                {
                    'type': 'cores',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'billing_type': 'fixed',
                }
            ],
            'plans': [
                {
                    'name': 'small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'prices': {'cores': 10},
                    'quotas': {'cores': 10},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering = models.Offering.objects.get(uuid=response.data['uuid'])
        plan = offering.plans.first()
        component = plan.components.get(component__type='cores')

        self.assertEqual(plan.unit_price, 100)
        self.assertEqual(component.amount, 10)

    def test_component_name_should_not_contain_spaces(self):
        plans_request = {
            'components': [
                {
                    'type': 'vCPU cores',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'billing_type': 'fixed',
                }
            ],
            'plans': [
                {
                    'name': 'small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'prices': {'cores': 10},
                    'quotas': {'cores': 10},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_usage_based_components_are_ignored_for_unit_price_computing(self):
        plans_request = {
            'components': [
                {
                    'type': 'cores',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'billing_type': 'usage',
                }
            ],
            'plans': [
                {
                    'name': 'Small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'prices': {'cores': 10},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering = models.Offering.objects.get(uuid=response.data['uuid'])
        plan = offering.plans.first()
        self.assertEqual(plan.unit_price, 0)

    def test_quotas_are_not_allowed_for_usage_based_components(self):
        plans_request = {
            'components': [
                {
                    'billing_type': 'usage',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'type': 'cores',
                }
            ],
            'plans': [
                {
                    'name': 'Small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'prices': {'cores': 10},
                    'quotas': {'cores': 10},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zero_quotas_are_allowed_for_fixed_components(self):
        plans_request = {
            'components': [
                {
                    'billing_type': 'fixed',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'type': 'cores',
                }
            ],
            'plans': [
                {
                    'name': 'Small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'prices': {'cores': 10},
                    'quotas': {'cores': 0},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_zero_price_could_be_skipped_for_fixed_components(self):
        plans_request = {
            'components': [
                {
                    'billing_type': 'fixed',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'type': 'cores',
                }
            ],
            'plans': [
                {
                    'name': 'Small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'quotas': {'cores': 10},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_invalid_price_components_are_not_allowed(self):
        plans_request = {
            'components': [
                {
                    'billing_type': 'fixed',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'type': 'cores',
                }
            ],
            'plans': [
                {
                    'name': 'Small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'quotas': {
                        'cores': 1,
                        'invalid_component': 10,
                    },
                    'prices': {
                        'cores': 1,
                        'invalid_component': 10,
                    },
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertTrue('Small' in response.data['plans'][0])
        self.assertTrue('invalid_component' in response.data['plans'][0])

    def test_create_offering_with_options(self):
        response = self.create_offering(
            'staff', attributes=True, add_payload={'options': OFFERING_OPTIONS}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.options, OFFERING_OPTIONS)

    def test_create_offering_with_invalid_options(self):
        options = {'foo': 'bar'}
        response = self.create_offering(
            'staff', attributes=True, add_payload={'options': options}
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_offering_with_invalid_type(self):
        response = self.create_offering(
            'staff', attributes=True, add_payload={'type': 'invalid'}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('type' in response.data)

    def test_validate_required_attribute(self):
        user = getattr(self.fixture, 'staff')
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        factories.ServiceProviderFactory(customer=self.customer)
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key='required_attribute', required=True
        )
        payload = {
            'name': 'offering',
            'category': factories.CategoryFactory.get_url(category),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'type': 'Support.OfferingTemplate',
            'attributes': {'vendorType': 'reseller'},
        }

        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertTrue(b'required_attribute' in response.content)

    def test_default_attribute_value_is_used_if_user_did_not_override_it(self):
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key='support_phone', default='support@example.com'
        )

        response = self.create_offering(
            'staff',
            add_payload={
                'category': factories.CategoryFactory.get_url(category),
                'attributes': {},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data['attributes']['support_phone'], 'support@example.com'
        )

    def test_default_attribute_value_is_not_used_if_user_has_overriden_it(self):
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key='support_phone', default='support@example.com'
        )

        response = self.create_offering(
            'staff',
            add_payload={
                'category': factories.CategoryFactory.get_url(category),
                'attributes': {'support_phone': 'admin@example.com'},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data['attributes']['support_phone'], 'admin@example.com'
        )

    def create_offering(self, user, attributes=False, add_payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        payload = {
            'name': 'offering',
            'category': factories.CategoryFactory.get_url(),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'type': 'Support.OfferingTemplate',  # This is used only for testing
            'plans': [
                {
                    'name': 'Small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                }
            ],
        }

        if attributes:
            payload['attributes'] = {
                'cloudDeploymentModel': 'private_cloud',
                'vendorType': 'reseller',
                'userSupportOptions': ['web_chat', 'phone'],
                'dataProtectionInternal': 'ipsec',
                'dataProtectionExternal': 'tls12',
            }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)

    def test_offering_creating_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()
        response = self.create_offering('owner')
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_offering_with_minimal_information_in_draft_state(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        for offering_type in list(manager.backends.keys()):
            payload = {
                'name': 'offering',
                'category': factories.CategoryFactory.get_url(),
                'customer': structure_factories.CustomerFactory.get_url(self.customer),
                'type': offering_type,
            }
            response = self.client.post(url, payload)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, offering_type
            )
            self.assertTrue(
                models.Offering.objects.filter(
                    customer=self.customer, type=offering_type
                ).exists()
            )
            offering = models.Offering.objects.filter(
                customer=self.customer, type=offering_type
            ).get()
            self.assertEqual(offering.state, models.Offering.States.DRAFT)


@ddt
class OfferingUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer, project=self.fixture.project, shared=True
        )
        self.url = factories.OfferingFactory.get_url(self.offering)
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING)
        add_permission(RoleEnum.CUSTOMER_MANAGER, PermissionEnum.UPDATE_OFFERING)
        add_permission(RoleEnum.OFFERING_MANAGER, PermissionEnum.UPDATE_OFFERING)

        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_ATTRIBUTES
        )
        add_permission(
            RoleEnum.CUSTOMER_MANAGER, PermissionEnum.UPDATE_OFFERING_ATTRIBUTES
        )
        add_permission(
            RoleEnum.OFFERING_MANAGER, PermissionEnum.UPDATE_OFFERING_ATTRIBUTES
        )

    @data('staff', 'owner')
    def test_staff_and_owner_can_update_offering_in_draft_state(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, 'new_offering')

    @data('customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_offering(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        'user',
    )
    def test_unrelated_user_can_not_update_offering(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        models.Offering.States.ACTIVE,
        models.Offering.States.PAUSED,
        models.Offering.States.ARCHIVED,
    )
    def test_owner_can_not_update_offering_in_active_or_paused_state(self, state):
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(models.Offering.States.ACTIVE, models.Offering.States.PAUSED)
    def test_staff_can_update_offering_in_active_or_paused_state(self, state):
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_not_update_offering_in_archived_state(self):
        # Arrange
        self.offering.state = models.Offering.States.ARCHIVED
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_authorized_user_can_update_offering_attributes_in_valid_state(self):
        self.fixture.service_manager = UserFactory()
        self.offering.add_user(self.fixture.service_manager)

        url = factories.OfferingFactory.get_url(self.offering, 'update_attributes')

        for state in (
            models.Offering.States.DRAFT,
            models.Offering.States.ACTIVE,
            models.Offering.States.PAUSED,
        ):
            for user in ('staff', 'owner', 'service_manager'):
                with self.subTest():
                    # Arrange
                    self.offering.state = state
                    self.offering.save()

                    # Act
                    self.client.force_authenticate(getattr(self.fixture, user))
                    response = self.client.post(url, {'key': 'value'})
                    self.assertEqual(response.status_code, status.HTTP_200_OK)

                    self.offering.refresh_from_db()
                    self.assertEqual(self.offering.attributes, {'key': 'value'})

    def test_authorized_user_can_not_update_offering_attributes_in_archived_state(self):
        self.fixture.service_manager = UserFactory()
        self.offering.add_user(self.fixture.service_manager)

        self.offering.state = models.Offering.States.ARCHIVED
        self.offering.save()

        url = factories.OfferingFactory.get_url(self.offering, 'update_attributes')

        for user in ('staff', 'owner', 'service_manager'):
            with self.subTest():
                self.client.force_authenticate(getattr(self.fixture, user))
                response = self.client.post(url, {'key': 'value'})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_updating_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_category_is_updated_attributes_are_validated(self):
        # Arrange
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key='userSupportOptions', required=True
        )

        # Act
        attributes = {'userSupportOptions': 'email'}
        category_url = factories.CategoryFactory.get_url(category)
        payload = {'category': category_url, 'attributes': attributes}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.category, category)

    def test_it_should_not_be_possible_to_delete_components_if_they_are_used(self):
        # Arrange
        factories.OfferingComponentFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering)

        # Act
        payload = {'components': []}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_it_should_be_possible_to_delete_components_if_they_are_not_used(self):
        # Arrange
        factories.OfferingComponentFactory(offering=self.offering)

        # Act
        payload = {'components': []}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(0, self.offering.components.count())

    def test_it_should_be_possible_to_create_new_components(self):
        # Act
        components = [
            {
                'type': 'cores',
                'name': 'Cores',
                'measured_unit': 'hours',
                'billing_type': 'fixed',
            }
        ]
        payload = {'components': components}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component = self.offering.components.get()
        self.assertEqual('cores', component.type)
        self.assertEqual('hours', component.measured_unit)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.FIXED, component.billing_type
        )

    def test_it_should_be_possible_to_update_existing_components(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            type='cores',
            name='CPU',
            measured_unit='H',
        )
        # Act
        components = [
            {
                'type': 'cores',
                'name': 'Cores',
                'measured_unit': 'hours',
                'billing_type': 'fixed',
            }
        ]
        payload = {'components': components}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component = self.offering.components.get()
        self.assertEqual('Cores', component.name)
        self.assertEqual('hours', component.measured_unit)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.FIXED, component.billing_type
        )

    def test_it_should_be_possible_to_update_plan_name(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering, name='Old name')

        # Act
        payload = {
            'plans': [
                {
                    'uuid': plan.uuid.hex,
                    'name': 'New name',
                }
            ]
        }
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertEqual(plan.name, 'New name')

    def test_it_should_be_possible_to_create_plan_components(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type='ram'
        )

        # Act
        payload = {
            'plans': [
                {
                    'uuid': plan.uuid.hex,
                    'quotas': {
                        'ram': 20,
                    },
                    'prices': {
                        'ram': 2,
                    },
                }
            ]
        }
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.amount, 20)
        self.assertEqual(plan_component.price, 2)

    def test_when_thumbnail_is_uploaded_plans_are_not_archived(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)

        # Act
        payload = {'thumbnail': dummy_image()}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload, format='multipart')

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan.refresh_from_db()
        self.assertFalse(plan.archived)

    def test_it_should_not_be_possible_to_remove_builtin_components(self):
        # Arrange
        self.offering.type = VIRTUAL_MACHINE_TYPE
        self.offering.save()

        cpu_component = factories.OfferingComponentFactory(
            offering=self.offering, type='cpu'
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {'components': []})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        cpu_component.refresh_from_db()

    def test_it_should_be_possible_to_update_plan_components(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type='ram'
        )
        plan_component = factories.PlanComponentFactory(
            plan=plan,
            component=offering_component,
            amount=10,
            price=1,
        )

        # Act
        payload = {
            'plans': [
                {
                    'uuid': plan.uuid.hex,
                    'quotas': {
                        'ram': 20,
                    },
                    'prices': {
                        'ram': 2,
                    },
                }
            ]
        }
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component.refresh_from_db()
        self.assertEqual(plan_component.amount, 20)
        self.assertEqual(plan_component.price, 2)

    def test_it_should_be_possible_to_archive_plan(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)

        # Act
        payload = {'plans': []}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertTrue(plan.archived)

    def test_it_should_be_possible_to_add_new_plan(self):
        payload = {
            'components': [
                {
                    'type': 'cores',
                    'name': 'Cores',
                    'measured_unit': 'hours',
                    'billing_type': 'fixed',
                }
            ],
            'plans': [
                {
                    'name': 'small',
                    'unit': UnitPriceMixin.Units.PER_MONTH,
                    'prices': {'cores': 10},
                    'quotas': {'cores': 10},
                }
            ],
        }
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(1, self.offering.plans.count())

    def test_update_offering_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {'backend_id': 'new_backend_id'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.backend_id, 'new_backend_id')

    def test_it_is_possible_to_update_offering_name_even_if_attributes_are_invalid(
        self,
    ):
        section = factories.SectionFactory(category=self.offering.category)
        attribute = factories.AttributeFactory(
            section=section, key='userSupportOptions', type='list'
        )
        models.AttributeOption.objects.create(
            attribute=attribute, key='web_chat', title='Web chat'
        )
        self.offering.attributes = {'userSupportOptions': ['invalid_value']}
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {'name': 'New name'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


@ddt
class OfferingPartialUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer, shared=True)
        self.url = factories.OfferingFactory.get_url(self.offering)

        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_ATTRIBUTES
        )
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_LOCATION)
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_DESCRIPTION
        )
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_OVERVIEW)
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_OPTIONS)
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_SECRET_OPTIONS
        )

    @data('staff', 'owner')
    def test_update_location(self, user):
        self.url = factories.OfferingFactory.get_url(self.offering, 'update_location')
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {'latitude': 1, 'longitude': 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.latitude, 1)
        self.assertEqual(self.offering.longitude, 2)

    @data('staff', 'owner')
    def test_update_description(self, user):
        self.url = factories.OfferingFactory.get_url(
            self.offering, 'update_description'
        )
        self.client.force_authenticate(getattr(self.fixture, user))
        new_category = factories.CategoryFactory()
        response = self.client.post(
            self.url, {'category': factories.CategoryFactory.get_url(new_category)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.category, new_category)

    @data('staff', 'owner')
    def test_update_overview(self, user):
        self.url = factories.OfferingFactory.get_url(self.offering, 'update_overview')
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {'name': 'new_name'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, 'new_name')

    @data('staff', 'owner')
    def test_update_options(self, user):
        self.url = factories.OfferingFactory.get_url(self.offering, 'update_options')
        self.client.force_authenticate(getattr(self.fixture, user))
        options = {
            'order': ['email'],
            'options': {
                'email': {
                    'type': 'string',
                    'label': 'email',
                    'default': 'user@example.com',
                    'required': False,
                }
            },
        }
        response = self.client.post(self.url, {'options': options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.options, options)

    @data('staff', 'owner')
    def test_update_secret_options(self, user):
        self.url = factories.OfferingFactory.get_url(
            self.offering, 'update_secret_options'
        )
        self.client.force_authenticate(getattr(self.fixture, user))
        secret_options = {
            'environ': [{'name': 'DJANGO_SETTINGS', 'value': 'settings.py'}],
            'language': 'python',
        }
        response = self.client.post(self.url, {'secret_options': secret_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.secret_options, secret_options)


@ddt
class OfferingDivisionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            project=self.fixture.project, customer=self.customer, shared=True
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, action='update_divisions'
        )
        self.delete_url = factories.OfferingFactory.get_url(
            self.offering, action='delete_divisions'
        )
        self.division = structure_factories.DivisionFactory()
        self.division_url = structure_factories.DivisionFactory.get_url(self.division)

    @data('staff', 'owner')
    def test_user_can_update_divisions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {'divisions': [self.division_url]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.divisions.count(), 1)

    @data('customer_support', 'admin', 'manager')
    def test_user_cannot_update_divisions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, {'divisions': [self.division_url]})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner')
    def test_user_can_delete_divisions(self, user):
        self.offering.divisions.add(self.division)
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.divisions.count(), 0)

    @data('customer_support', 'admin', 'manager')
    def test_user_cannot_delete_divisions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class OfferingDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer, project=self.fixture.project, shared=True
        )

    @data('staff', 'owner')
    def test_authorized_user_can_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Offering.objects.filter(customer=self.customer).exists()
        )

    @data('customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_offering_deleting_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()
        response = self.delete_offering('owner')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def delete_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.delete(url)
        return response


@ddt
class OfferingAttributesTest(test.APITransactionTestCase):
    def setUp(self):
        self.serializer = serializers.OfferingCreateSerializer()
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.attribute = factories.AttributeFactory(
            section=self.section, key='userSupportOptions', type='list'
        )
        models.AttributeOption.objects.create(
            attribute=self.attribute, key='web_chat', title='Web chat'
        ),
        models.AttributeOption.objects.create(
            attribute=self.attribute, key='phone', title='Telephone'
        )

    @data(['web_chat', 'phone'])
    def test_list_attribute_is_valid(self, value):
        self._valid('list', value)

    @data(['chat', 'phone'], 'web_chat', 1, False)
    def test_list_attribute_is_not_valid(self, value):
        self._not_valid('list', value)

    @data('web_chat')
    def test_choice_attribute_is_valid(self, value):
        self._valid('choice', value)

    @data(['web_chat'], 'chat', 1, False)
    def test_choice_attribute_is_not_valid(self, value):
        self._not_valid('choice', value)

    @data('name')
    def test_string_attribute_is_valid(self, value):
        self._valid('string', value)

    @data(['web_chat'], 1, False)
    def test_string_attribute_is_not_valid(self, value):
        self._not_valid('string', value)

    def test_integer_attribute_is_valid(self):
        self._valid('integer', 1)

    @data(['web_chat'], 'web_chat', -1)
    def test_integer_attribute_is_not_valid(self, value):
        self._not_valid('integer', value)

    def test_boolean_attribute_is_valid(self):
        self._valid('boolean', True)

    @data(['web_chat'], 'web_chat', 1)
    def test_boolean_attribute_is_not_valid(self, value):
        self._not_valid('boolean', value)

    def _valid(self, attribute_type, value):
        self.attribute.type = attribute_type
        self.attribute.save()
        attributes = {
            'attributes': {
                'userSupportOptions': value,
            },
            'category': self.category,
        }
        self.assertIsNone(self.serializer._validate_attributes(attributes))

    def _not_valid(self, attribute_type, value):
        self.attribute.type = attribute_type
        self.attribute.save()
        attributes = {
            'attributes': {
                'userSupportOptions': value,
            },
            'category': self.category,
        }
        self.assertRaises(
            rest_exceptions.ValidationError,
            self.serializer._validate_attributes,
            attributes,
        )


class OfferingQuotaTest(test.APITransactionTestCase):
    def get_usage(self, category):
        return category.quotas.get(name='offering_count').usage

    def test_empty_category(self):
        self.assertEqual(0, self.get_usage(factories.CategoryFactory()))

    def test_active_offerings_are_counted(self):
        category = factories.CategoryFactory()
        provider = factories.ServiceProviderFactory()
        factories.OfferingFactory.create_batch(
            3,
            category=category,
            customer=provider.customer,
            state=models.Offering.States.ACTIVE,
        )
        self.assertEqual(3, self.get_usage(category))

    def test_draft_offerings_are_not_counted(self):
        category = factories.CategoryFactory()
        provider = factories.ServiceProviderFactory()
        factories.OfferingFactory.create_batch(
            2,
            category=category,
            customer=provider.customer,
            state=models.Offering.States.DRAFT,
        )
        self.assertEqual(0, self.get_usage(category))


@ddt
class OfferingStateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer, project=self.fixture.project, shared=True
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.fixture.service_manager = UserFactory()
        self.offering.add_user(self.fixture.service_manager)

        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.PAUSE_OFFERING)
        add_permission(RoleEnum.CUSTOMER_MANAGER, PermissionEnum.PAUSE_OFFERING)

        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UNPAUSE_OFFERING)
        add_permission(RoleEnum.CUSTOMER_MANAGER, PermissionEnum.UNPAUSE_OFFERING)

    @data(
        'staff',
    )
    def test_authorized_user_can_activate_offering(self, user):
        response, offering = self.update_offering_state(user, 'activate')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, offering.States.ACTIVE)

    @data('owner', 'user', 'customer_support', 'admin', 'manager', 'service_manager')
    def test_unauthorized_user_can_not_activate_offering(self, user):
        response, offering = self.update_offering_state(user, 'activate')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, offering.States.DRAFT)

    @data('owner', 'service_manager')
    def test_authorized_user_can_pause_offering(self, user):
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()

        response, offering = self.update_offering_state(user, 'pause')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, models.Offering.States.PAUSED)

    @data('customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_pause_offering(self, user):
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()

        response, offering = self.update_offering_state(user, 'pause')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(offering.state, offering.States.ACTIVE)

    @data('owner', 'service_manager')
    def test_authorized_user_can_unpause_offering(self, user):
        self.offering.state = models.Offering.States.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, 'unpause')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, models.Offering.States.ACTIVE)

    @data('customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_unpause_offering(self, user):
        self.offering.state = models.Offering.States.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, 'unpause')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, models.Offering.States.PAUSED)

    def test_invalid_state(self):
        response, offering = self.update_offering_state('staff', 'pause')
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(offering.state, offering.States.DRAFT)

    @data('activate', 'pause', 'archive')
    def test_offering_state_changing_is_not_available_for_blocked_organization(
        self, state
    ):
        self.customer.blocked = True
        self.customer.save()
        response, offering = self.update_offering_state('staff', state)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_provide_paused_reason(self):
        # Arrange
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()

        # Act
        response, offering = self.update_offering_state(
            'staff', 'pause', {'paused_reason': 'Not available anymore.'}
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.paused_reason, 'Not available anymore.')

    def test_authorized_user_can_not_activate_offering_without_plans(self):
        self.plan.delete()
        response, _ = self.update_offering_state('staff', 'activate')
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    @data('owner', 'service_manager')
    def test_authorized_user_can_not_unpause_offering_without_plans(self, user):
        self.plan.delete()
        self.offering.state = models.Offering.States.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, 'unpause')
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def update_offering_state(self, user, state, payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, state)
        response = self.client.post(url, payload)
        self.offering.refresh_from_db()

        return response, self.offering


@ddt
class OfferingPublicGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offerings = [
            factories.OfferingFactory(state=models.Offering.States.ACTIVE),
            factories.OfferingFactory(state=models.Offering.States.DRAFT),
            factories.OfferingFactory(
                state=models.Offering.States.PAUSED, shared=False
            ),
            factories.OfferingFactory(
                state=models.Offering.States.ACTIVE, shared=False
            ),
        ]
        factories.PlanFactory(offering=self.offerings[-1])

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_cannot_view_offerings(self):
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_anonymous_cannot_view_draft_offerings(self):
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertNotEqual(models.Offering.States.DRAFT, offering['state'])

    def test_anonymous_cannot_view_offering_scope(self):
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertNotIn('scope', offering)

    def test_anonymous_can_view_offering_scope(self):
        url = factories.OfferingFactory.get_public_url(self.offerings[0])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('staff', 'owner', 'user', 'customer_support', 'admin')
    def test_authenticated_user_can_view_offering_scope(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertIn('scope', offering)

    @data('owner', 'user', 'customer_support', 'admin', 'manager', None)
    def test_private_offerings_are_hidden_and_shared_offering_visible(self, user):
        if user:
            user = getattr(self.fixture, user)
            self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        shared_exists = None
        private_exists = None

        for offering in response.data:
            if offering['shared']:
                shared_exists = True
            else:
                private_exists = True

        self.assertTrue(shared_exists)
        self.assertFalse(private_exists)

    @data('staff', 'global_support')
    def test_private_offerings_and_shared_offering_are_visible(self, user):
        if user:
            user = getattr(self.fixture, user)
            self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        shared_exists = None
        private_exists = None

        for offering in response.data:
            if offering['shared']:
                shared_exists = True
            else:
                private_exists = True

        self.assertTrue(shared_exists)
        self.assertTrue(private_exists)

    @data('owner', 'customer_support', 'admin', 'manager')
    def test_private_offerings_are_visible_for_related_user(self, user):
        private_offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=False,
            customer=self.fixture.customer,
            project=self.fixture.project,
        )
        factories.PlanFactory(offering=private_offering)

        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(len(response.data), 2)

        shared_exists = None
        private_exists = None

        for offering in response.data:
            if offering['shared']:
                shared_exists = True
            else:
                private_exists = True

        self.assertTrue(shared_exists)
        self.assertTrue(private_exists)

    def test_anonymous_can_get_offerings(self):
        offering_list_url = factories.OfferingFactory.get_public_list_url()
        result = self.client.get(offering_list_url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(len(result.data), 1)


class OfferingExportImportTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.temp_dir = tempfile.gettempdir()

    def test_export_offering(self):
        offering = factories.OfferingFactory(
            description='Описание с non-ASCII символами.'
        )
        export_offering(offering, self.temp_dir)
        json_path = os.path.join(self.temp_dir, offering.uuid.hex + '.json')
        self.assertTrue(os.path.exists(json_path))

    def test_export_offering_with_thumbnail(self):
        GIF = 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'

        with open(os.path.join(self.temp_dir, 'pic.gif'), 'wb') as pic:
            pic.write(base64.b64decode(GIF))

        offering = factories.OfferingFactory(thumbnail=pic.name)
        export_offering(offering, self.temp_dir)
        filename, file_extension = os.path.splitext(offering.thumbnail.file.name)
        pic_path = os.path.join(self.temp_dir, offering.uuid.hex + file_extension)
        self.assertTrue(os.path.exists(pic_path))

    def test_import_offering(self):
        export_data = self._get_data()
        create_offering(export_data, self.fixture.customer)

        self.assertTrue(
            models.Offering.objects.filter(
                customer=self.fixture.customer, name='offering_name'
            ).exists()
        )
        offering = models.Offering.objects.filter(
            customer=self.fixture.customer, name='offering_name'
        ).get()
        self.assertTrue(offering.thumbnail)
        self.assertEqual(offering.plans.count(), 1)
        self.assertEqual(offering.plans.first().name, 'Start')
        self.assertEqual(offering.plans.first().components.count(), 1)
        self.assertEqual(offering.components.count(), 1)
        self.assertEqual(offering.components.first().type, 'node')

    def test_update_offering(self):
        export_data = self._get_data()
        offering = create_offering(export_data, self.fixture.customer)
        export_data['name'] = 'new_offering_name'
        export_data['plans'][0]['name'] = 'new_plan'
        export_data['components'][0]['type'] = 'new_type'
        export_data['plans'][0]['components'][0]['component']['type'] = 'new_type'

        update_offering(offering, export_data)
        offering.refresh_from_db()
        self.assertEqual(offering.name, 'new_offering_name')
        self.assertEqual(offering.plans.first().name, 'new_plan')
        self.assertEqual(offering.components.first().type, 'new_type')

    def _get_data(self):
        path = os.path.abspath(os.path.dirname(__file__))
        data = json.loads(
            pkg_resources.resource_stream(__name__, 'offering.json').read().decode()
        )
        category = factories.CategoryFactory()
        data['category_id'] = category.id

        thumbnail = data.get('thumbnail')
        if thumbnail:
            data['thumbnail'] = os.path.join(os.path.dirname(path), thumbnail)

        return data


class OfferingDoiTest(test.APITransactionTestCase):
    def setUp(self):
        self.dc_resp = json.loads(
            pkg_resources.resource_stream(__name__, 'datacite-resp.json')
            .read()
            .decode()
        )['data']
        self.ref_pids = [
            x['relatedIdentifier']
            for x in self.dc_resp['attributes']['relatedIdentifiers']
        ]
        self.offering = factories.OfferingFactory(
            datacite_doi='10.15159/t9zh-k971',
            citation_count=self.dc_resp['attributes']['citationCount'],
        )
        self.offering_referral = factories.OfferingReferralFactory(scope=self.offering)
        self.offering2 = factories.OfferingFactory(
            datacite_doi='10.15159/t9zh-k972',
            citation_count=0,
        )
        self.offering_referral2 = factories.OfferingReferralFactory(
            scope=self.offering2
        )
        self.fixture = fixtures.ProjectFixture()

    def test_viewing_datacite_related_fields(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url).json()

        self.assertEqual(response['datacite_doi'], self.dc_resp['id'])
        self.assertEqual(
            response['citation_count'], self.dc_resp['attributes']['citationCount']
        )

    def test_authenticated_user_can_lookup_offering_referrals(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingReferralFactory.get_list_url()

        response = self.client.get(
            url, {'scope': factories.OfferingFactory.get_url(self.offering)}
        ).json()

        self.assertTrue('pid' in response[0])
        self.assertTrue(len(response) == 1)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_user_cannot_lookup_offering_referrals(self):
        url = factories.OfferingReferralFactory.get_list_url()

        response = self.client.get(
            url, {'scope': factories.OfferingFactory.get_url(self.offering)}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class OfferingThumbnailTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()
        self.url = factories.OfferingFactory.get_url(
            offering=self.offering, action='update_thumbnail'
        )
        self.url_delete = factories.OfferingFactory.get_url(
            offering=self.offering, action='delete_thumbnail'
        )
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_THUMBNAIL
        )
        add_permission(
            RoleEnum.CUSTOMER_MANAGER, PermissionEnum.UPDATE_OFFERING_THUMBNAIL
        )

    @data('staff')
    def test_staff_can_update_or_delete_thumbnail_of_archived_offering(self, user):
        self.offering.state = models.Offering.States.ARCHIVED
        self.offering.save()
        self._user_have_access(user)

    @data('offering_owner', 'service_manager', 'offering_admin', 'offering_manager')
    def test_user_cannot_update_or_delete_thumbnail_of_archived_offering(self, user):
        self.offering.state = models.Offering.States.ARCHIVED
        self.offering.save()
        self._user_does_not_have_access(user)

    @data('staff', 'offering_owner', 'service_manager')
    def test_user_can_update_or_delete_thumbnail(self, user):
        self._user_have_access(user)

    @data('offering_admin', 'offering_manager')
    def test_user_cannot_update_or_delete_thumbnail(self, user):
        self._user_does_not_have_access(user)

    def _user_have_access(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, {'thumbnail': dummy_image()}, format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.thumbnail)

        response = self.client.post(self.url_delete)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.offering.refresh_from_db()
        self.assertFalse(self.offering.thumbnail)

    def _user_does_not_have_access(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, {'thumbnail': dummy_image()}, format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering.refresh_from_db()
        self.assertFalse(self.offering.thumbnail)

        response = self.client.post(self.url_delete)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class OfferingComponentsUpdateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering_component = self.fixture.offering_component
        self.url = factories.OfferingFactory.get_url(self.offering, 'update_components')
        factories.OfferingComponentFactory(
            offering=self.offering,
            type='gpu',
        )
        resource = self.fixture.resource
        resource.delete()
        add_permission(
            RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING_COMPONENTS
        )
        add_permission(
            RoleEnum.CUSTOMER_MANAGER, PermissionEnum.UPDATE_OFFERING_COMPONENTS
        )

    @data('offering_owner', 'service_manager')
    def test_offering_components_update_succeed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = [
            {
                'billing_type': models.OfferingComponent.BillingTypes.USAGE,
                'type': 'cpu',
                'name': 'CPU',
                'measured_unit': 'cpu_k_hours',
            },
            {
                'billing_type': models.OfferingComponent.BillingTypes.USAGE,
                'type': 'ram',
                'name': 'RAM',
                'measured_unit': 'gb_hours',
            },
        ]

        response = self.client.post(self.url, payload)
        self.assertEqual(200, response.status_code)
        offering_components = models.OfferingComponent.objects.filter(
            offering=self.offering
        )
        self.assertEqual(2, offering_components.count())
        offering_component_names = set(
            offering_components.values_list('type', flat=True)
        )
        self.assertEqual({'cpu', 'ram'}, offering_component_names)

    @data('offering_manager', 'offering_admin')
    def test_offering_components_update_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, [])

        self.assertEqual(403, response.status_code)
