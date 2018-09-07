from __future__ import unicode_literals

import json

from ddt import data, ddt
from rest_framework import exceptions as rest_exceptions
from rest_framework import test, status

from waldur_core.core.tests.utils import PostgreSQLTest
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS

from . import factories
from .. import serializers


@ddt
class OfferingGetTest(PostgreSQLTest):

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


class OfferingFilterTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        attributes = {
            'cloudDeploymentModel': 'private_cloud',
            'userSupportOption': ['phone'],
        }
        self.offering = factories.OfferingFactory(customer=self.fixture.customer,
                                                  attributes=attributes)
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_choice_positive(self):
        response = self.client.get(self.url, {'attributes': json.dumps({
            'cloudDeploymentModel': 'private_cloud',
        })})
        self.assertEqual(len(response.data), 1)

    def test_filter_choice_negative(self):
        response = self.client.get(self.url, {'attributes': json.dumps({
            'cloudDeploymentModel': 'public_cloud',
        })})
        self.assertEqual(len(response.data), 0)

    def test_filter_list_positive(self):
        """
        If an attribute is a list, we use multiple choices.
        """
        factories.OfferingFactory(attributes={
            'userSupportOption': ['phone', 'email', 'fax'],
        })
        factories.OfferingFactory(attributes={
            'userSupportOption': ['email'],
        })
        response = self.client.get(self.url, {'attributes': json.dumps({
            'userSupportOption': ['fax', 'email'],
        })})
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


@ddt
class OfferingCreateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_validate_correct_geolocations(self):
        response = self.create_offering('staff', add_payload={'geolocations': [{'latitude': 123, 'longitude': 345}]})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_validate_uncorrect_geolocations(self):
        response = self.create_offering('staff', add_payload={'geolocations': [{'longitude': 345}]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('geolocations' in response.data.keys())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_offering_with_attributes(self):
        response = self.create_offering('staff', attributes=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.attributes, {
            'cloudDeploymentModel': 'private_cloud',
            'vendorType': 'reseller',
            'userSupportOptions': ['web_chat', 'phone'],
            'dataProtectionInternal': 'ipsec',
            'dataProtectionExternal': 'tls12'
        })

    def test_dont_create_offering_if_attributes_is_not_valid(self):
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.attribute = factories.AttributeFactory(section=self.section, key='userSupportOptions')
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()

        payload = {
            'name': 'offering',
            'native_name': 'native_name',
            'category': factories.CategoryFactory.get_url(category=self.category),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
            'attributes': json.dumps({
                'cloudDeploymentModel': 'private_cloud',
                'vendorType': 'reseller',
                'userSupportOptions': ['chat', 'phone'],
                'dataProtectionInternal': 'ipsec',
                'dataProtectionExternal': 'tls12'
            })
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
            'attributes': '"String is not allowed, dictionary is expected."'
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_with_plans(self):
        plans_request = {
            'plans': [
                {'name': 'small',
                 'description': 'CPU 1',
                 'unit': UnitPriceMixin.Units.QUANTITY,
                 'unit_price': 100}
            ]
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data['plans']), 1)

    def test_create_offering_with_plan_components(self):
        plans_request = {
            'plans': [
                {
                    'name': 'small',
                    'description': 'CPU 1',
                    'unit': UnitPriceMixin.Units.QUANTITY,
                    'unit_price': 100,
                    'components': [
                        {
                            'type': 'cores',
                            'amount': 10,
                            'price': 10,
                        }
                    ]
                }
            ]
        }
        response = self.create_offering('owner', add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['plans'][0]['components'][0]['amount'], 10)

    def test_create_offering_with_options(self):
        response = self.create_offering('staff', attributes=True, add_payload={'options': OFFERING_OPTIONS})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.options, OFFERING_OPTIONS)

    def test_create_offering_with_invalid_options(self):
        options = {
            'foo': 'bar'
        }
        response = self.create_offering('staff', attributes=True, add_payload={'options': options})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_create_offering_with_invalid_type(self):
        response = self.create_offering('staff', attributes=True, add_payload={'type': 'invalid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('type' in response.data)

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
        }

        if attributes:
            payload['attributes'] = {
                'cloudDeploymentModel': 'private_cloud',
                'vendorType': 'reseller',
                'userSupportOptions': ['web_chat', 'phone'],
                'dataProtectionInternal': 'ipsec',
                'dataProtectionExternal': 'tls12'
            }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)


@ddt
class OfferingUpdateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_offering(self, user):
        response, offering = self.update_offering(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.name, 'new_offering')
        self.assertTrue(models.Offering.objects.filter(name='new_offering').exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_offering(self, user):
        response, offering = self.update_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        factories.ServiceProviderFactory(customer=self.customer)
        offering = factories.OfferingFactory(customer=self.customer, shared=True)
        url = factories.OfferingFactory.get_url(offering)

        response = self.client.patch(url, {
            'name': 'new_offering'
        })
        offering.refresh_from_db()

        return response, offering


@ddt
class OfferingDeleteTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer, shared=True)

    @data('staff', 'owner')
    def test_authorized_user_can_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, response.data)
        self.assertFalse(models.Offering.objects.filter(customer=self.customer).exists())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def delete_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.delete(url)
        return response


@ddt
class OfferingAttributesTest(test.APITransactionTestCase):

    def setUp(self):
        self.serializer = serializers.OfferingSerializer()
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.attribute = factories.AttributeFactory(
            section=self.section,
            key='userSupportOptions',
            type='list'
        )
        models.AttributeOption.objects.create(
            attribute=self.attribute,
            key='web_chat',
            title='Web chat'),
        models.AttributeOption.objects.create(
            attribute=self.attribute,
            key='phone',
            title='Telephone'
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

    @data(['web_chat'], 'web_chat', False)
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
            'userSupportOptions': value,
        }
        self.assertIsNone(self.serializer._validate_attributes(attributes, self.category))

    def _not_valid(self, attribute_type, value):
        self.attribute.type = attribute_type
        self.attribute.save()
        attributes = {
            'userSupportOptions': value,
        }
        self.assertRaises(rest_exceptions.ValidationError, self.serializer._validate_attributes,
                          attributes, self.category)


class OfferingQuotaTest(PostgreSQLTest):
    def get_usage(self, category):
        return category.quotas.get(name='offering_count').usage

    def test_empty_category(self):
        self.assertEqual(0, self.get_usage(factories.CategoryFactory()))

    def test_offering_count_quota_is_populated(self):
        category = factories.CategoryFactory()
        provider = factories.ServiceProviderFactory()
        factories.OfferingFactory.create_batch(3, category=category, customer=provider.customer)
        self.assertEqual(3, self.get_usage(category))


@ddt
class OfferingStateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_state(self, user):
        response, offering = self.update_offering_state(user, 'activate')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, offering.States.ACTIVE)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_state(self, user):
        response, offering = self.update_offering_state(user, 'activate')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(offering.state, offering.States.DRAFT)

    def test_invalid_state(self):
        response, offering = self.update_offering_state('staff', 'pause')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(offering.state, offering.States.DRAFT)

    def update_offering_state(self, user, state):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        factories.ServiceProviderFactory(customer=self.customer)
        offering = factories.OfferingFactory(customer=self.customer, shared=True)
        url = factories.OfferingFactory.get_url(offering, state)
        response = self.client.post(url)
        offering.refresh_from_db()

        return response, offering


class AllowedCustomersTest(PostgreSQLTest):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    def test_staff_can_update_allowed_customers(self):
        url = structure_factories.CustomerFactory.get_url(self.customer, 'offerings')
        user = getattr(self.fixture, 'staff')
        self.client.force_authenticate(user)
        response = self.client.post(url, {
            "offering_set": [
                factories.OfferingFactory.get_url(),
                factories.OfferingFactory.get_url(),
            ]
        })
        self.customer.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(self.customer.offering_set.all()), 2)

    def test_other_users_not_can_update_allowed_customers(self):
        url = structure_factories.CustomerFactory.get_url(self.customer, 'offerings')
        user = getattr(self.fixture, 'owner')
        self.client.force_authenticate(user)
        response = self.client.post(url, {
            "offering_set": [
                factories.OfferingFactory.get_url(),
                factories.OfferingFactory.get_url(),
            ]
        })
        self.customer.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(len(self.customer.offering_set.all()), 0)
