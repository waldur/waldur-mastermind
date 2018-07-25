from tempfile import NamedTemporaryFile
from zipfile import ZipFile

from ddt import data, ddt
from rest_framework import status
from rest_framework.test import APITransactionTestCase
from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


@ddt
class PlaybookPermissionsTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = ProjectFixture()
        self.playbook = factories.PlaybookFactory()

    def test_anonymous_user_cannot_retrieve_playbook(self):
        response = self.client.get(factories.PlaybookFactory.get_url(self.playbook))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data('user', 'staff', 'global_support',
          'owner', 'customer_support',
          'admin', 'manager', 'project_support')
    def test_user_can_retrieve_playbook(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(factories.PlaybookFactory.get_url(self.playbook))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_user_can_create_playbook(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload()
        response = self.client.post(factories.PlaybookFactory.get_list_url(), data=payload, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('user', 'global_support',
          'owner', 'customer_support',
          'admin', 'manager', 'project_support')
    def test_user_cannot_create_playbook(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload()
        response = self.client.post(factories.PlaybookFactory.get_list_url(), data=payload, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_user_can_update_playbook(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {'name': 'test playbook 2'}
        response = self.client.put(factories.PlaybookFactory.get_url(self.playbook), data=payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.playbook.refresh_from_db()
        self.assertEqual(self.playbook.name, payload['name'])

    @data('user', 'global_support',
          'owner', 'customer_support',
          'admin', 'manager', 'project_support')
    def test_user_cannot_update_playbook(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {'name': 'test playbook 2'}
        response = self.client.put(factories.PlaybookFactory.get_url(self.playbook), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_user_can_delete_playbook(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(factories.PlaybookFactory.get_url(self.playbook))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('user', 'global_support',
          'owner', 'customer_support',
          'admin', 'manager', 'project_support')
    def test_user_cannot_delete_playbook(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(factories.PlaybookFactory.get_url(self.playbook))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def _get_valid_payload(self):
        temp_file = NamedTemporaryFile(suffix='.zip')
        zip_file = ZipFile(temp_file, 'w')
        zip_file.writestr('main.yml', 'test')
        zip_file.close()
        temp_file.seek(0)

        return {
            'name': 'test playbook',
            'archive': temp_file,
            'entrypoint': 'main.yml',
            'parameters': [
                {
                    'name': 'parameter1',
                },
                {
                    'name': 'parameter2',
                },
            ]
        }
