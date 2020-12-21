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

from waldur_core.media.utils import dummy_image
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.fixtures import ServiceFixture
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace_vmware import VIRTUAL_MACHINE_TYPE

from .. import serializers
from ..management.commands.export_offering import export_offering
from ..management.commands.import_offering import create_offering, update_offering
from . import factories
from .helpers import override_marketplace_settings


@ddt
class OfferingGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(shared=True)

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_offerings_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_offerings_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class SecretOptionsTests(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        self.url = factories.OfferingFactory.get_url(self.offering)

    @data('staff', 'owner')
    def test_secret_options_are_visible_to_authorized_user(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('secret_options' in response.data)

    @data('user', 'customer_support', 'admin', 'manager')
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
            {'attributes': json.dumps({'cloudDeploymentModel': 'private_cloud',})},
        )
        self.assertEqual(len(response.data), 1)

    def test_filter_choice_negative(self):
        response = self.client.get(
            self.url,
            {'attributes': json.dumps({'cloudDeploymentModel': 'public_cloud',})},
        )
        self.assertEqual(len(response.data), 0)

    def test_filter_list_positive(self):
        """
        If an attribute is a list, we use multiple choices.
        """
        factories.OfferingFactory(
            attributes={'userSupportOption': ['phone', 'email', 'fax'],}
        )
        factories.OfferingFactory(
            attributes={'userSupportOption': ['email'],}
        )
        response = self.client.get(
            self.url,
            {'attributes': json.dumps({'userSupportOption': ['fax', 'email'],})},
        )
        self.assertEqual(len(response.data), 2)

    def test_shared_offerings_are_available_for_all_users(self):
        # Arrange
        factories.OfferingFactory(customer=self.fixture.customer, shared=False)
        self.offering.shared = True
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 1)

    def test_private_offerings_are_available_for_users_in_allowed_customers(self):
        fixture = fixtures.CustomerFixture()
        self.offering.allowed_customers.add(fixture.customer)

        self.client.force_authenticate(fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offerings_are_not_available_for_users_in_other_customers(self):
        fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_available_for_users_in_allowed_projects(self):
        fixture = fixtures.ProjectFixture()
        self.offering.allowed_customers.add(fixture.customer)

        self.client.force_authenticate(fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

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

    def test_filter_offerings_by_project(self):
        fixture = ServiceFixture()
        self.offering.scope = fixture.service_settings
        self.offering.save()
        fixture.service_project_link

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {'project_uuid': fixture.project.uuid.hex})
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            self.url, {'project_uuid': self.fixture.project.uuid.hex}
        )
        self.assertEqual(len(response.data), 0)


@ddt
class OfferingCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_options_default_value(self):
        self.create_offering('staff')
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.options, {'options': {}, 'order': []})

    def test_validate_correct_geolocations(self):
        response = self.create_offering(
            'staff', add_payload={'latitude': 123, 'longitude': 345}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_offering_with_attributes(self):
        response = self.create_offering('staff', attributes=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering = models.Offering.objects.get(uuid=response.data['uuid'])
        self.assertFalse(offering.scope.shared)

    def test_create_offering_with_plans(self):
        plans_request = {'plans': [{'name': 'Small', 'description': 'Basic plan',}]}
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data['plans']), 1)

    def test_specify_max_amount_for_plan(self):
        plans_request = {
            'plans': [{'name': 'Small', 'description': 'Basic plan', 'max_amount': 10,}]
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['plans'][0]['max_amount'], 10)

    def test_max_amount_should_be_at_least_one(self):
        plans_request = {
            'plans': [{'name': 'Small', 'description': 'Basic plan', 'max_amount': -1,}]
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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
                    'quotas': {'cores': 1, 'invalid_component': 10,},
                    'prices': {'cores': 1, 'invalid_component': 10,},
                }
            ],
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
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
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
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
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
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
            'plans': [{'name': 'Small', 'unit': UnitPriceMixin.Units.PER_MONTH,}],
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
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class OfferingUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer, shared=True)
        self.url = factories.OfferingFactory.get_url(self.offering)

    @data('staff', 'owner')
    def test_staff_and_owner_can_update_offering_in_draft_state(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, 'new_offering')

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_offering(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url, {'name': 'new_offering'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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

    def test_if_category_is_updated_required_attributes_are_validated(self):
        # Arrange
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key='userSupportOptions', required=True
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        category_url = factories.CategoryFactory.get_url(category)
        response = self.client.patch(self.url, {'category': category_url})

        # Assert
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
            offering=self.offering, type='cores', name='CPU', measured_unit='H',
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
        payload = {'plans': [{'uuid': plan.uuid.hex, 'name': 'New name',}]}
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
                {'uuid': plan.uuid.hex, 'quotas': {'ram': 20,}, 'prices': {'ram': 2,}}
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
            plan=plan, component=offering_component, amount=10, price=1,
        )

        # Act
        payload = {
            'plans': [
                {'uuid': plan.uuid.hex, 'quotas': {'ram': 20,}, 'prices': {'ram': 2,}}
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


@ddt
class OfferingDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer, shared=True)

    @data('staff', 'owner')
    def test_authorized_user_can_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Offering.objects.filter(customer=self.customer).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
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
            'attributes': {'userSupportOptions': value,},
            'category': self.category,
        }
        self.assertIsNone(self.serializer._validate_attributes(attributes))

    def _not_valid(self, attribute_type, value):
        self.attribute.type = attribute_type
        self.attribute.save()
        attributes = {
            'attributes': {'userSupportOptions': value,},
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
class OfferingCountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.category = factories.CategoryFactory()
        self.url = factories.CategoryFactory.get_url(self.category)

    def assert_count(self, user, value, shared=False):
        factories.OfferingFactory.create_batch(
            2,
            customer=self.customer,
            category=self.category,
            shared=shared,
            state=models.Offering.States.ACTIVE,
        )
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(value, response.data['offering_count'])

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_see_private_offering(self, user):
        self.assert_count(getattr(self.fixture, user), 2)

    @data('owner', 'admin', 'manager')
    def test_unauthorized_user_can_not_see_private_offering(self, user):
        self.assert_count(getattr(fixtures.ProjectFixture(), user), 0)

    @data('staff', 'owner', 'admin', 'manager')
    def test_anyone_can_see_public_offering(self, user):
        self.assert_count(getattr(fixtures.ProjectFixture(), user), 2, shared=True)


@ddt
class OfferingStateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer, shared=True)
        self.fixture.service_manager = UserFactory()
        self.offering.add_user(self.fixture.service_manager)

    @data('staff',)
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

    @data('user', 'customer_support', 'admin', 'manager')
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

    @data('user', 'customer_support', 'admin', 'manager')
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

    def update_offering_state(self, user, state, payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, state)
        response = self.client.post(url, payload)
        self.offering.refresh_from_db()

        return response, self.offering


class AllowedCustomersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    def test_staff_can_update_allowed_customers(self):
        url = structure_factories.CustomerFactory.get_url(self.customer, 'offerings')
        user = getattr(self.fixture, 'staff')
        self.client.force_authenticate(user)
        response = self.client.post(
            url,
            {
                "offering_set": [
                    factories.OfferingFactory.get_url(),
                    factories.OfferingFactory.get_url(),
                ]
            },
        )
        self.customer.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(self.customer.offering_set.all()), 2)

    def test_other_users_not_can_update_allowed_customers(self):
        url = structure_factories.CustomerFactory.get_url(self.customer, 'offerings')
        user = getattr(self.fixture, 'owner')
        self.client.force_authenticate(user)
        response = self.client.post(
            url,
            {
                "offering_set": [
                    factories.OfferingFactory.get_url(),
                    factories.OfferingFactory.get_url(),
                ]
            },
        )
        self.customer.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(len(self.customer.offering_set.all()), 0)


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
        ]

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_cannot_view_offerings(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_anonymous_cannot_view_draft_offerings(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertNotEqual(models.Offering.States.DRAFT, offering['state'])

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_anonymous_cannot_view_offering_scope(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertNotIn('scope', offering)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_anonymous_can_view_offering_scope(self):
        url = factories.OfferingFactory.get_url(self.offerings[0])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    @data('staff', 'owner', 'user', 'customer_support', 'admin')
    def test_authenticated_user_can_view_offering_scope(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertIn('scope', offering)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager', None)
    def test_private_offerings_are_hidden(self, user):
        if user:
            user = getattr(self.fixture, user)
            self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertTrue('shared', offering)


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
            datacite_doi='10.15159/t9zh-k972', citation_count=0,
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

    def test_anonymous_user_cannot_lookup_offering_referrals(self):
        url = factories.OfferingReferralFactory.get_list_url()

        response = self.client.get(
            url, {'scope': factories.OfferingFactory.get_url(self.offering)}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
