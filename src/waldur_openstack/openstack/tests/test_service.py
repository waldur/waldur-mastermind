from ddt import ddt, data
from mock import patch
from rest_framework import status, test

from waldur_core.structure.models import ServiceSettings, ProjectRole, CustomerRole
from waldur_core.structure.tests import factories as structure_factories

from .. import models
from . import factories, fixtures


class BaseServiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.customers = {
            'owned': structure_factories.CustomerFactory(),
            'has_admined_project': structure_factories.CustomerFactory(),
            'has_managed_project': structure_factories.CustomerFactory(),
        }

        self.users = {
            'customer_owner': structure_factories.UserFactory(),
            'project_admin': structure_factories.UserFactory(),
            'project_manager': structure_factories.UserFactory(),
            'no_role': structure_factories.UserFactory(),
        }

        self.projects = {
            'owned': structure_factories.ProjectFactory(customer=self.customers['owned']),
            'admined': structure_factories.ProjectFactory(customer=self.customers['has_admined_project']),
            'managed': structure_factories.ProjectFactory(customer=self.customers['has_managed_project']),
        }

        self.services = {
            'owned': factories.OpenStackServiceFactory(customer=self.customers['owned']),
            'admined': factories.OpenStackServiceFactory(customer=self.customers['has_admined_project']),
            'managed': factories.OpenStackServiceFactory(customer=self.customers['has_managed_project']),
            'not_in_project': factories.OpenStackServiceFactory(),
        }

        self.settings = structure_factories.ServiceSettingsFactory(type="OpenStack", customer=self.customers['owned'])
        self.customers['owned'].add_user(self.users['customer_owner'], CustomerRole.OWNER)

        self.projects['admined'].add_user(self.users['project_admin'], ProjectRole.ADMINISTRATOR)
        self.projects['managed'].add_user(self.users['project_manager'], ProjectRole.MANAGER)

        factories.OpenStackServiceProjectLinkFactory(service=self.services['admined'], project=self.projects['admined'])
        factories.OpenStackServiceProjectLinkFactory(service=self.services['managed'], project=self.projects['managed'])


@ddt
class ListServiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.service = self.fixture.openstack_service
        self.spl = self.fixture.openstack_spl

    def get_list(self):
        return self.client.get(factories.OpenStackServiceFactory.get_list_url())

    def test_anonymous_user_cannot_list_services(self):
        response = self.get_list()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_list_services(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.get_list()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        service_url = factories.OpenStackServiceFactory.get_url(self.service)
        self.assertIn(service_url, [instance['url'] for instance in response.data])

    def test_user_cannot_list_services_of_projects_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.fixture.user)

        response = self.get_list()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, len(response.data))


class GetServiceTest(BaseServiceTest):
    def test_anonymous_user_cannot_access_service(self):
        for service_type in 'admined', 'managed', 'not_in_project':
            response = self.client.get(factories.OpenStackServiceFactory.get_url(self.services[service_type]))
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_access_service_allowed_for_project_he_is_administrator_of(self):
        self.client.force_authenticate(user=self.users['project_admin'])

        response = self.client.get(factories.OpenStackServiceFactory.get_url(self.services['admined']))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_can_access_service_allowed_for_project_he_is_manager_of(self):
        self.client.force_authenticate(user=self.users['project_manager'])

        response = self.client.get(factories.OpenStackServiceFactory.get_url(self.services['managed']))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_can_see_services_customer_name(self):
        self.client.force_authenticate(user=self.users['project_admin'])

        response = self.client.get(factories.OpenStackServiceFactory.get_url(self.services['admined']))

        customer = self.services['admined'].customer

        self.assertIn('customer', response.data)
        self.assertEqual(structure_factories.CustomerFactory.get_url(customer), response.data['customer'])

        self.assertIn('customer_name', response.data)
        self.assertEqual(customer.name, response.data['customer_name'])

    def test_user_cannot_access_service_allowed_for_project_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.users['no_role'])

        for service_type in 'admined', 'managed':
            response = self.client.get(factories.OpenStackServiceFactory.get_url(self.services[service_type]))
            # 404 is used instead of 403 to hide the fact that the resource exists at all
            self.assertEqual(
                response.status_code,
                status.HTTP_404_NOT_FOUND,
                'User (role=none) should not see service (type=' + service_type + ')',
            )

    def test_user_cannot_access_service_not_allowed_for_any_project(self):
        for user_role in 'customer_owner', 'project_admin', 'project_manager':
            self.client.force_authenticate(user=self.users[user_role])

            response = self.client.get(factories.OpenStackServiceFactory.get_url(self.services['not_in_project']))
            # 404 is used instead of 403 to hide the fact that the resource exists at all
            self.assertEqual(
                response.status_code,
                status.HTTP_404_NOT_FOUND,
                'User (role=' + user_role + ') should not see service (type=not_in_project)',
            )


class CreateServiceTest(BaseServiceTest):

    @patch('waldur_core.structure.models.ServiceSettings.get_backend')
    def test_user_can_add_service_to_the_customer_he_owns(self, mocked_backend):
        mocked_backend().check_admin_tenant.return_value = True
        self.client.force_authenticate(user=self.users['customer_owner'])

        payload = self._get_owned_payload()

        with patch('waldur_core.structure.executors.ServiceSettingsCreateExecutor.execute') as mocked:
            response = self.client.post(factories.OpenStackServiceFactory.get_list_url(), payload)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

            settings = ServiceSettings.objects.get(name=payload['name'])
            self.assertFalse(settings.shared)

            mocked.assert_any_call(settings)
            mocked_backend().ping.assert_called_once()

    @patch('waldur_core.structure.models.ServiceSettings.get_backend')
    def test_admin_service_credentials_are_validated(self, mocked_backend):
        mocked_backend().check_admin_tenant.return_value = False
        self.client.force_authenticate(user=self.users['customer_owner'])

        payload = self._get_owned_payload()
        response = self.client.post(factories.OpenStackServiceFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'],
                         ['Provided credentials are not for admin tenant.'])

    def test_user_cannot_add_service_to_the_customer_he_sees_but_doesnt_own(self):
        choices = {
            'project_admin': 'has_admined_project',
            'project_manager': 'has_managed_project',
        }
        for user_role, customer_type in choices.items():
            self.client.force_authenticate(user=self.users[user_role])

            new_service = factories.OpenStackServiceFactory.build(
                settings=self.settings, customer=self.customers[customer_type])
            response = self.client.post(factories.OpenStackServiceFactory.get_list_url(),
                                        self._get_valid_payload(new_service))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_add_service_to_the_customer_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.users['no_role'])

        new_service = factories.OpenStackServiceFactory(customer=self.customers['owned'])
        response = self.client.post(factories.OpenStackServiceFactory.get_list_url(),
                                    self._get_valid_payload(new_service))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def _get_owned_payload(self):
        return {
            'name': 'service_settings name',
            'customer': structure_factories.CustomerFactory.get_url(self.customers['owned']),
            'backend_url': 'http://example.com',
            'username': 'user',
            'password': 'secret',
            'tenant_name': 'admin',
        }

    def _get_valid_payload(self, service):
        return {
            'name': service.settings.name,
            'settings': structure_factories.ServiceSettingsFactory.get_url(service.settings),
            'customer': structure_factories.CustomerFactory.get_url(service.customer),
        }


class UpdateServiceTest(BaseServiceTest):

    def test_user_cannot_change_customer_of_service_he_owns(self):
        user = self.users['customer_owner']

        self.client.force_authenticate(user=user)

        service = self.services['owned']

        new_customer = structure_factories.CustomerFactory()
        new_customer.add_user(user, CustomerRole.OWNER)

        payload = {'customer': structure_factories.CustomerFactory.get_url(new_customer)}
        response = self.client.patch(factories.OpenStackServiceFactory.get_url(service), data=payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        reread_service = models.OpenStackService.objects.get(pk=service.pk)
        self.assertEqual(reread_service.customer, service.customer)
