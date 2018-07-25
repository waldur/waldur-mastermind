from rest_framework import test, status

from waldur_core.structure.models import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class ServiceProjectLinkPermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.users = {
            'owner': structure_factories.UserFactory(),
            'admin': structure_factories.UserFactory(),
            'manager': structure_factories.UserFactory(),
            'no_role': structure_factories.UserFactory(),
            'not_connected': structure_factories.UserFactory(),
        }

        # a single customer
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.users['owner'], CustomerRole.OWNER)

        # that has 3 users connected: admin, manager
        self.connected_project = structure_factories.ProjectFactory(customer=self.customer)
        self.connected_project.add_user(self.users['admin'], ProjectRole.ADMINISTRATOR)
        self.connected_project.add_user(self.users['manager'], ProjectRole.MANAGER)

        # has defined a service and connected service to a project
        self.service = factories.OpenStackServiceFactory(customer=self.customer)
        self.service_project_link = factories.OpenStackServiceProjectLinkFactory(
            project=self.connected_project,
            service=self.service)

        # the customer also has another project with users but without a permission link
        self.not_connected_project = structure_factories.ProjectFactory(customer=self.customer)
        self.not_connected_project.add_user(self.users['not_connected'], ProjectRole.ADMINISTRATOR)
        self.not_connected_project.save()

        self.url = factories.OpenStackServiceProjectLinkFactory.get_list_url()

    def test_anonymous_user_cannot_grant_service_to_project(self):
        response = self.client.post(self.url, self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_connect_service_and_project_he_owns(self):
        user = self.users['owner']
        self.client.force_authenticate(user=user)

        service = factories.OpenStackServiceFactory(customer=self.customer)
        project = structure_factories.ProjectFactory(customer=self.customer)

        payload = self._get_valid_payload(service, project)

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_admin_cannot_connect_new_service_and_project_if_he_is_project_admin(self):
        user = self.users['admin']
        self.client.force_authenticate(user=user)

        service = factories.OpenStackServiceFactory(customer=self.customer)
        project = self.connected_project
        payload = self._get_valid_payload(service, project)

        response = self.client.post(self.url, payload)
        # the new service should not be visible to the user
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertDictContainsSubset(
            {'service': ['Invalid hyperlink - Object does not exist.']}, response.data)

    def test_user_cannot_revoke_service_and_project_permission_if_he_is_project_manager(self):
        user = self.users['manager']
        self.client.force_authenticate(user=user)

        url = factories.OpenStackServiceProjectLinkFactory.get_url(self.service_project_link)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def _get_valid_payload(self, service=None, project=None):
        return {
            'service': factories.OpenStackServiceFactory.get_url(service),
            'project': structure_factories.ProjectFactory.get_url(project)
        }
