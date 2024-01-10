import unittest
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests.factories import ProjectFactory, ServiceSettingsFactory
from waldur_jira import executors, models
from waldur_jira.tests import factories, fixtures


class ProjectBaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.JiraFixture()


@ddt
class ProjectGetTest(ProjectBaseTest):
    @data("owner", "admin", "manager")
    def test_user_with_access_can_access_project(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.fixture.jira_project_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_without_access_cannot_access_project(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.fixture.jira_project_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
@mock.patch("waldur_jira.executors.ProjectCreateExecutor.execute")
class ProjectCreateTest(ProjectBaseTest):
    def test_user_can_create_project(self, executor):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url(), self.get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        executor.assert_called_once()

    @data("key with spaces", "T0000LONGKEY")
    def test_user_can_not_create_project_with_invalid_key(self, key, executor):
        self.client.force_authenticate(self.fixture.staff)
        payload = self.get_valid_payload()
        payload.update(dict(key=key))
        response = self.client.post(self.get_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def get_url(self):
        return factories.ProjectFactory.get_list_url()

    def get_valid_payload(self):
        return {
            "name": "Test project",
            "key": "TST",
            "template": self.fixture.jira_project_template_url,
            "service_settings": ServiceSettingsFactory.get_url(
                self.fixture.service_settings
            ),
            "project": ProjectFactory.get_url(self.fixture.project),
        }


@ddt
@mock.patch("waldur_jira.executors.ProjectDeleteExecutor.execute")
class ProjectDeleteTest(ProjectBaseTest):
    @data(
        "staff",
    )
    def test_staff_can_delete_project(self, user, executor):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.fixture.jira_project_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        executor.assert_called_once()

    @data("owner", "admin", "manager")
    def test_other_users_cannot_delete_project(self, user, executor):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.fixture.jira_project_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BaseProjectImportTest(test.APITransactionTestCase):
    def _generate_backend_projects(self, count=1):
        projects = []
        for i in range(count):
            project = factories.ProjectFactory()
            project.delete()
            projects.append(project)

        return projects


@unittest.skip("Move import to marketplace")
class ProjectImportableResourcesTest(BaseProjectImportTest):
    def setUp(self):
        super().setUp()
        self.url = factories.ProjectFactory.get_list_url("importable_resources")
        self.fixture = fixtures.JiraFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch("waldur_jira.backend.JiraBackend.get_resources_for_import")
    def test_importable_projects_are_returned(self, get_projects_mock):
        backend_projects = self._generate_backend_projects()
        get_projects_mock.return_value = backend_projects
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(backend_projects))
        returned_backend_ids = [item["backend_id"] for item in response.data]
        expected_backend_ids = [item.backend_id for item in backend_projects]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))
        get_projects_mock.assert_called()


@unittest.skip("Move import to marketplace")
class ProjectImportResourceTest(BaseProjectImportTest):
    def setUp(self):
        super().setUp()
        self.url = factories.ProjectFactory.get_list_url("import_resource")
        self.fixture = fixtures.JiraFixture()
        self.client.force_authenticate(self.fixture.owner)

        self.jira_patcher_get_project = mock.patch(
            "waldur_jira.backend.JiraBackend.get_project"
        )
        self.jira_mock_get_project = self.jira_patcher_get_project.start()

        def get_project(backend_id):
            return self._generate_backend_projects()[0]

        self.jira_mock_get_project.side_effect = get_project

        self.jira_patcher_executors = mock.patch("waldur_jira.serializers.executors")
        self.jira_mock_executors = self.jira_patcher_executors.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_backend_project_is_imported(self):
        backend_id = "backend_id"

        payload = {
            "backend_id": backend_id,
            "project": self.fixture.project.uuid,
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            self.jira_mock_executors.ProjectPullExecutor.execute.call_count, 1
        )

    def test_backend_project_cannot_be_imported_if_it_is_registered_in_waldur(self):
        project = factories.ProjectFactory(
            service_settings=self.fixture.service_settings,
            project=self.fixture.project,
        )

        payload = {
            "backend_id": project.backend_id,
            "project": self.fixture.project.uuid,
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TasksTest(BaseProjectImportTest):
    def setUp(self):
        super().setUp()

        self.url = factories.ProjectFactory.get_list_url("import_resource")
        self.fixture = fixtures.JiraFixture()
        self.client.force_authenticate(self.fixture.owner)

        self.jira_patcher_get_project = mock.patch(
            "waldur_jira.backend.JiraBackend.get_project"
        )
        self.jira_mock_get_project = self.jira_patcher_get_project.start()

        def get_project(backend_id):
            return self._generate_backend_projects()[0]

        self.jira_mock_get_project.side_effect = get_project

        self.jira_patcher_get_issues_count = mock.patch(
            "waldur_jira.backend.JiraBackend.get_issues_count"
        )
        self.jira_mock_get_issues_count = self.jira_patcher_get_issues_count.start()
        self.jira_mock_get_issues_count.return_value = 1

        self.jira_patcher_import_project_batch = mock.patch(
            "waldur_jira.backend.JiraBackend.import_project_issues"
        )
        self.jira_mock_import_project_batch = (
            self.jira_patcher_import_project_batch.start()
        )

    def tearDown(self):
        mock.patch.stopall()

    def test_import_projects(self):
        project = factories.ProjectFactory()
        executors.ProjectPullExecutor.execute(project, is_async=False)
        project.refresh_from_db()
        self.assertEqual(project.state, models.Project.States.OK)
        self.assertEqual(project.runtime_state, "success")
