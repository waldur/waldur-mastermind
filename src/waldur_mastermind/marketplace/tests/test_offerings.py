from __future__ import unicode_literals

import json

from ddt import data, ddt
from rest_framework import exceptions as rest_exceptions
from rest_framework import test, status

from waldur_core.core.tests.utils import PostgreSQLTest
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models

from . import factories
from .. import serializers


@ddt
class OfferingGetTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory()

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
class OfferingCreateTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_create_offering(self, user):
        response = self.create_offering(user)
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

    def create_offering(self, user, attributes=False, add_payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        payload = {
            'name': 'offering',
            'category': factories.CategoryFactory.get_url(),
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
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
        offering = factories.OfferingFactory(customer=self.customer)
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
        self.offering = factories.OfferingFactory(customer=self.customer)

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


class OfferingFilterTest(PostgreSQLTest):

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(attributes={
            'cloudDeploymentModel': 'private_cloud',
            'userSupportOption': ['phone'],
        })
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_positive(self):
        response = self.client.get(self.url, {'attributes': json.dumps({
            'cloudDeploymentModel': 'private_cloud',
        })})
        self.assertEqual(len(response.data), 1)

    def test_filter_negative(self):
        response = self.client.get(self.url, {'attributes': json.dumps({
            'cloudDeploymentModel': 'private_cloud_1',
        })})
        self.assertEqual(len(response.data), 0)
