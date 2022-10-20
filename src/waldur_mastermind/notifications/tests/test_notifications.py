from rest_framework import status, test

from waldur_core.structure.models import ProjectRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.notifications.tests import factories


class DryRunBroadcastMessageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.url = factories.BroadcastMessageFactory.get_list_url(action='dry_run')

    def test_dry_run(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            'query': {
                'projects': [self.project.uuid.hex],
                'project_roles': [ProjectRole.ADMINISTRATOR],
            }
        }
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, 0)

        self.fixture.admin
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, 1)


class UsersBroadcastMessageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.url = factories.BroadcastMessageFactory.get_list_url(action='users')
        self.fixture.admin

    def test_users(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            'query': {
                'projects': [self.project.uuid.hex],
                'project_roles': [ProjectRole.ADMINISTRATOR],
            }
        }
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['project_users']), 1)
        self.assertEqual(
            response.data['project_users'][0]['email'], self.fixture.admin.email
        )
