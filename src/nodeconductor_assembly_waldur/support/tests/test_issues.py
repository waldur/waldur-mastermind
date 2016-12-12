from django.conf import settings
from rest_framework import test, status

from nodeconductor.structure.tests import fixtures as structure_fixtures, factories as structure_factories

from . import factories


class IssueCrudTest(test.APITransactionTestCase):
    def setUp(self):
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND'] = 'SupportBackend'
        self.fixture = structure_fixtures.ProjectFixture()

    def test_staff_can_list_issues(self):
        self.client.force_authenticate(self.fixture.staff)
        issue = factories.IssueFactory()
        response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], issue.uuid.hex)

    def test_staff_can_create_issue(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(factories.IssueFactory.get_list_url(), {
            'summary': 'Unable to provision VM',
            'reporter_user': structure_factories.UserFactory.get_url(self.fixture.staff),
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_can_update_issue(self):
        self.client.force_authenticate(self.fixture.staff)
        issue = factories.IssueFactory(summary='Old summary')
        response = self.client.put(factories.IssueFactory.get_url(issue), {
            'summary': 'New summary',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['summary'], 'New summary')

    def test_staff_can_delete_issue(self):
        self.client.force_authenticate(self.fixture.staff)
        issue = factories.IssueFactory()
        response = self.client.delete(factories.IssueFactory.get_url(issue))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
