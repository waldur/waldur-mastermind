from ddt import data, ddt
from django.core import mail
from rest_framework import status, test

from waldur_core.core.utils import format_homeport_link
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings

from . import factories


@ddt
class ServiceProviderGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )

    @data('staff', 'owner', 'user', 'customer_support', 'admin', 'manager')
    def test_service_provider_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_service_provider_should_be_visible_to_unauthenticated_users_by_default(
        self,
    ):
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_service_provider_should_be_invisible_to_unauthenticated_users_when_offerings_are_public(
        self,
    ):
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'owner')
    def test_service_provider_api_secret_code_is_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(
            self.service_provider, 'api_secret_code'
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('api_secret_code' in response.data.keys())

    @data('user', 'customer_support', 'admin', 'manager')
    def test_service_provider_api_secret_code_is_invisible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(
            self.service_provider, 'api_secret_code'
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ServiceProviderRegisterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff')
    def test_staff_can_register_a_service_provider(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_register_an_service_provider(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_marketplace_settings(OWNER_CAN_REGISTER_SERVICE_PROVIDER=True)
    @data('owner')
    def test_owner_can_register_service_provider_with_settings_enabled(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_marketplace_settings(OWNER_CAN_REGISTER_SERVICE_PROVIDER=True)
    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_register_service_provider_with_settings_enabled(
        self, user
    ):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('owner')
    def test_owner_can_not_register_service_provider_with_settings_disabled(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_register_service_provider_with_settings_disabled(
        self, user
    ):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_service_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_list_url()

        payload = {
            'customer': structure_factories.CustomerFactory.get_url(self.customer),
        }

        return self.client.post(url, payload)


@ddt
class ServiceProviderUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data('staff', 'owner')
    def test_authorized_user_can_update_service_provider(self, user):
        response, service_provider = self.update_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(service_provider.enable_notifications)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_update_service_provider(self, user):
        response, service_provider = self.update_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_service_provider(self, user, payload=None):
        if not payload:
            payload = {'enable_notifications': False}

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(service_provider)

        response = self.client.patch(url, payload)
        service_provider.refresh_from_db()

        return response, service_provider

    @data('staff', 'owner')
    def test_generate_api_secret_code(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        url = factories.ServiceProviderFactory.get_url(
            service_provider, 'api_secret_code'
        )
        old_secret_code = service_provider.api_secret_code
        response = self.client.post(url)
        service_provider.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(service_provider.api_secret_code, old_secret_code)

    @data('user', 'customer_support', 'admin', 'manager')
    def test_not_generate_api_secret_code(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        url = factories.ServiceProviderFactory.get_url(
            service_provider, 'api_secret_code'
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ServiceProviderDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)

    @data('staff', 'owner')
    def test_authorized_user_can_delete_service_provider(self, user):
        response = self.delete_service_provider(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def test_service_provider_could_not_be_deleted_if_it_has_active_offerings(self):
        factories.OfferingFactory(
            customer=self.customer, state=models.Offering.States.ACTIVE
        )
        response = self.delete_service_provider('staff')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def test_service_provider_is_deleted_if_it_has_archived_offering(self):
        factories.OfferingFactory(
            customer=self.customer, state=models.Offering.States.ARCHIVED
        )
        response = self.delete_service_provider('staff')
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    @data('user', 'customer_support', 'admin', 'manager')
    def test_unauthorized_user_can_not_delete_service_provider(self, user):
        response = self.delete_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def delete_service_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(self.service_provider)
        response = self.client.delete(url)
        return response


class CustomerSerializerTest(test.APITransactionTestCase):
    def test_service_provider_is_not_defined(self):
        customer = structure_factories.CustomerFactory()
        self.assertFalse(self.get_value(customer))

    def test_service_provider_is_defined(self):
        customer = factories.ServiceProviderFactory().customer
        self.assertTrue(self.get_value(customer))

    def get_value(self, customer):
        user = structure_factories.UserFactory(is_staff=True)
        url = structure_factories.CustomerFactory.get_url(customer)

        self.client.force_login(user)
        response = self.client.get(url)
        return response.data['is_service_provider']


class ServiceProviderNotificationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.fixture.owner
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type='Support.OfferingTemplate'
        )
        self.component = factories.OfferingComponentFactory(
            billing_type=models.OfferingComponent.BillingTypes.USAGE, offering=offering
        )

        self.resource = factories.ResourceFactory(
            offering=offering, state=models.Resource.States.OK, name='My resource'
        )

    def test_get_customer_if_usages_are_not_exist(self):
        self.assertEqual(len(utils.get_info_about_missing_usage_reports()), 1)
        self.assertEqual(
            utils.get_info_about_missing_usage_reports()[0]['customer'],
            self.fixture.customer,
        )

    def test_do_not_get_customer_if_usages_are_exist(self):
        factories.ComponentUsageFactory(
            resource=self.resource, component=self.component
        )
        self.assertEqual(len(utils.get_info_about_missing_usage_reports()), 0)

    def test_usages_notification_message(self):
        tasks.send_notifications_about_usages()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.fixture.owner.email])
        self.assertEqual(
            mail.outbox[0].subject, 'Reminder about missing usage reports.'
        )
        self.assertTrue('My resource' in mail.outbox[0].body)
        public_resources_url = format_homeport_link(
            'organizations/{organization_uuid}/marketplace-public-resources/',
            organization_uuid=self.fixture.customer.uuid,
        )
        self.assertTrue(public_resources_url in mail.outbox[0].body)
