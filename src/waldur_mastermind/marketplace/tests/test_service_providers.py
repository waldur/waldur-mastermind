import re

from ddt import data, ddt
from django.core import mail
from rest_framework import status, test

from waldur_core.core.utils import format_homeport_link
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings
from waldur_mastermind.marketplace_support import PLUGIN_NAME

from . import factories


@ddt
class ServiceProviderGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider

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

    @data('staff', 'offering_owner')
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

    def test_user_projects_are_visible(self):
        self.fixture.resource
        self.fixture.manager
        self.client.force_authenticate(self.fixture.service_owner)
        url = factories.ServiceProviderFactory.get_url(
            self.fixture.service_provider, 'users'
        )
        response = self.client.get(url)
        self.assertEqual(response.json()[0]['projects_count'], 1)


@ddt
class ServiceProviderRegisterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
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

    def test_owner_can_register_service_provider_with_settings_enabled(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.REGISTER_SERVICE_PROVIDER)
        response = self.create_service_provider('owner')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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
        self.fixture = structure_fixtures.ProjectFixture()
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

    def update_service_provider(self, user, payload=None, **kwargs):
        if not payload:
            payload = {'enable_notifications': False}

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(service_provider)

        response = self.client.patch(url, payload, **kwargs)
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

    def test_upload_service_provider_image(self):
        payload = {'image': dummy_image()}
        response, service_provider = self.update_service_provider(
            'staff', payload=payload, format='multipart'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(service_provider.image)

        url = factories.ServiceProviderFactory.get_url(service_provider)
        response = self.client.patch(url, {'image': None})
        service_provider.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(service_provider.image)


@ddt
class ServiceProviderDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
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
        self.fixture = structure_fixtures.CustomerFixture()
        self.fixture.owner
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
            name='First',
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
        other_offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
            name='Second',
        )
        factories.OfferingComponentFactory(
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            offering=other_offering,
        )

        factories.ResourceFactory(
            offering=other_offering,
            state=models.Resource.States.OK,
            name='Second resource',
        )

        factories.ResourceFactory(
            offering=other_offering,
            state=models.Resource.States.OK,
            name='Third resource',
        )

        event_type = 'notification_usages'
        structure_factories.NotificationFactory(key=f"marketplace.{event_type}")
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
        body = re.sub(r'\s+|\n+', ' ', mail.outbox[0].body)
        self.assertTrue(public_resources_url in body)
        self.assertTrue('1. First: - My resource' in body)
        self.assertTrue('2. Second: - Second resource - Third resource' in body)


class ConsumerProjectListTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action='projects'
        )

    def test_service_provider_can_view_project_with_purchased_resource(self):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(
            self.consumer_project.uuid.hex, [item['uuid'] for item in response.data]
        )


class ConsumerSshKeyListTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.admin = self.mp_fixture.admin
        self.ssh_key = structure_factories.SshPublicKeyFactory(
            user=self.admin,
            is_shared=True,
        )
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action='keys'
        )

    def test_service_provider_can_view_ssh_keys_from_project_with_purchased_resource(
        self,
    ):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(self.ssh_key.uuid.hex, [item['uuid'] for item in response.data])


class ConsumerProjectPermissionListTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.admin = self.mp_fixture.admin
        self.permission = get_permissions(self.consumer_project, self.admin).get()
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action='project_permissions'
        )

    def test_service_provider_can_view_project_permissions_in_project_with_purchased_resource(
        self,
    ):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(self.permission.id, [item['pk'] for item in response.data])


class ConsumerUserListTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.admin = self.mp_fixture.admin
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action='users'
        )

    def test_service_provider_can_view_users_in_project_with_purchased_resource(self):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(self.admin.uuid.hex, [item['uuid'] for item in response.data])

    def test_disabled_users_are_excluded(self):
        # Arrange
        self.admin.is_active = False
        self.admin.save()

        # Act
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(200, response.status_code)
        self.assertNotIn(self.admin.uuid.hex, [item['uuid'] for item in response.data])


class SetOfferingUsersTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.fixture.project
        self.consumable_resource = self.fixture.resource
        self.offering = self.fixture.offering
        self.admin = self.fixture.admin
        self.url = factories.ServiceProviderFactory.get_url(
            self.fixture.service_provider,
            action='set_offerings_username',
        )

    def test_offering_user_creation(self):
        self.assertEqual(
            0,
            models.OfferingUser.objects.filter(
                user=self.admin, offering=self.offering
            ).count(),
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                'user_uuid': self.admin.uuid,
                'username': 'SET_OFFERING_USERNAME',
            },
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(
            1,
            models.OfferingUser.objects.filter(
                user=self.admin, offering=self.offering
            ).count(),
        )
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        self.assertEqual('SET_OFFERING_USERNAME', offering_user.username)

    def test_offering_user_update(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.admin,
            username='ADMIN_OLD',
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                'user_uuid': self.admin.uuid,
                'username': 'ADMIN_NEW',
            },
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(
            1,
            models.OfferingUser.objects.filter(
                user=self.admin, offering=self.offering
            ).count(),
        )
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        self.assertEqual('ADMIN_NEW', offering_user.username)


class ServiceProviderUserCustomersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        self.url = factories.ServiceProviderFactory.get_url(
            self.service_provider, 'user_customers'
        )

    def test_get_user_customers_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_user_uuid(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
            name='First',
        )

        resource = factories.ResourceFactory(
            offering=offering, state=models.Resource.States.OK, name='My resource'
        )
        resource.project.add_user(self.fixture.user, ProjectRole.ADMIN)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {'user_uuid': self.fixture.user.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
