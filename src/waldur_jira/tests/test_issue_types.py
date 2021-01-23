from unittest import mock

from rest_framework import test

from waldur_jira import models
from waldur_jira.backend import JiraBackend
from waldur_jira.tests import factories, fixtures


class IssueTypesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.JiraFixture()

        self.mock_issue_type = mock.MagicMock()
        self.mock_issue_type.configure_mock(
            id='100',
            description='Basic issue type',
            iconUrl='http://example.com/1.svg',
            subtask=False,
        )
        self.mock_issue_type.name = 'Task'
        self.mock_project = mock.MagicMock(issueTypes=[self.mock_issue_type])

        self.backend = JiraBackend(self.fixture.service_settings)
        self.backend.get_project = lambda _: self.mock_project

    def test_create_new_issue_type(self):
        self.backend.pull_issue_types(self.fixture.jira_project)
        self.assert_issue_types_is_pulled()

    def test_update_existing_issue_type(self):
        issue_type = factories.IssueTypeFactory(
            settings=self.fixture.service_settings, backend_id=self.mock_issue_type.id,
        )
        self.fixture.jira_project.issue_types.add(issue_type)

        self.backend.pull_issue_types(self.fixture.jira_project)
        self.assert_issue_types_is_pulled()

    def test_delete_stale_issue_type(self):
        self.mock_project.configure_mock(issueTypes=[])
        self.backend.pull_issue_types(self.fixture.jira_project)
        self.assertEqual(self.fixture.jira_project.issue_types.count(), 0)

    def assert_issue_types_is_pulled(self):
        issue_type = models.IssueType.objects.get(
            settings=self.fixture.service_settings, backend_id=self.mock_issue_type.id
        )

        self.assertEqual(self.fixture.jira_project.issue_types.count(), 1)
        self.assertEqual(issue_type.name, self.mock_issue_type.name)
        self.assertEqual(issue_type.description, self.mock_issue_type.description)
        self.assertEqual(issue_type.icon_url, self.mock_issue_type.iconUrl)
