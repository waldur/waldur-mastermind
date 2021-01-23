from unittest import mock

from django.conf import settings
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_jira import models
from waldur_jira.tests import factories, fixtures


class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.JiraFixture()
        self.author = self.fixture.manager
        self.non_author = self.fixture.user

        self.issue = factories.IssueFactory(
            project=self.fixture.jira_project,
            state=models.Issue.States.OK,
            user=self.author,
        )
        self.issue_url = factories.IssueFactory.get_url(self.issue)


class IssueGetTest(BaseTest):
    def test_staff_can_list_all_issues(self):
        """
        Issues without author are listed too.
        """
        issue_without_user = factories.IssueFactory()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertTrue(
            issue_without_user.uuid.hex in [issue['uuid'] for issue in response.data]
        )
        self.assertTrue(
            self.issue.uuid.hex in [issue['uuid'] for issue in response.data]
        )

    def test_author_can_list_its_own_issues(self):
        self.client.force_authenticate(self.author)
        response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(
            self.issue.uuid.hex in [issue['uuid'] for issue in response.data]
        )

    def test_non_author_can_not_list_other_issues(self):
        self.client.force_authenticate(self.non_author)
        response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_staff_can_get_issue(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.issue_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_author_can_get_issue(self):
        self.client.force_authenticate(self.author)
        response = self.client.get(self.issue_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_filter_issues_by_statuses(self):
        self.client.force_authenticate(self.fixture.staff)
        factories.IssueFactory(
            project=self.fixture.jira_project, status='OK',
        )
        factories.IssueFactory(
            project=self.fixture.jira_project, status='NOTOK',
        )

        response = self.client.get(factories.IssueFactory.get_list_url() + "?status=OK")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            factories.IssueFactory.get_list_url() + "?status=OK&status=NOTOK"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_staff_can_filter_issues_by_priorities(self):
        self.client.force_authenticate(self.fixture.staff)
        factories.IssueFactory(
            project=self.fixture.jira_project,
            priority=factories.PriorityFactory(name='HIGH'),
        )
        factories.IssueFactory(
            project=self.fixture.jira_project,
            priority=factories.PriorityFactory(name='LOW'),
        )

        response = self.client.get(
            factories.IssueFactory.get_list_url() + "?priority_name=HIGH"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            factories.IssueFactory.get_list_url()
            + "?priority_name=HIGH&priority_name=LOW"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_non_author_can_not_get_issue(self):
        self.client.force_authenticate(self.non_author)
        response = self.client.get(self.issue_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class IssueCreateBaseTest(BaseTest):
    def setUp(self):
        super(IssueCreateBaseTest, self).setUp()
        self.fixture.jira_project.issue_types.add(self.fixture.issue_type)

        self.jira_patcher = mock.patch('waldur_jira.backend.JIRA')
        self.jira_mock = self.jira_patcher.start()
        self.create_issue = self.jira_mock().create_issue

        class Object:
            pass

        mock_priority = Object()
        mock_priority.id = self.fixture.priority.backend_id
        mock_issue_type = Object()
        mock_issue_type.id = self.fixture.issue_type.backend_id
        ttr_field_id = 'customfield_10138'
        self.ttr_value = 10000
        self.create_issue.return_value = mock.Mock(
            **{
                'key': 'backend_id',
                'fields.assignee.name': '',
                'fields.assignee.emailAddress': '',
                'fields.assignee.displayName': '',
                'fields.creator.name': '',
                'fields.creator.emailAddress': '',
                'fields.creator.displayName': '',
                'fields.reporter.name': '',
                'fields.reporter.emailAddress': '',
                'fields.reporter.displayName': '',
                'fields.resolutiondate': '',
                'fields.summary': '',
                'fields.description': '',
                'fields.status.name': '',
                'fields.resolution': '',
                'fields.priority': mock_priority,
                'fields.issuetype': mock_issue_type,
                'fields.%s.ongoingCycle.remainingTime.millis'
                % ttr_field_id: self.ttr_value,
            }
        )
        self.jira_mock().issue.return_value = self.create_issue.return_value
        self.jira_mock().fields.return_value = [
            {
                'clauseNames': ['Time to resolution'],
                'id': ttr_field_id,
                'name': 'Time to resolution',
            }
        ]

    def tearDown(self):
        super(IssueCreateBaseTest, self).tearDown()
        mock.patch.stopall()

    def _get_issue_payload(self, **kwargs):
        payload = {
            'jira_project': self.fixture.jira_project_url,
            'summary': 'Summary',
            'description': 'description test issue',
            'priority': self.fixture.priority_url,
            'type': self.fixture.issue_type_url,
        }
        payload.update(kwargs)
        return payload


class IssueCreateResourceTest(IssueCreateBaseTest):
    def setUp(self):
        super(IssueCreateResourceTest, self).setUp()
        self.resource = structure_factories.TestNewInstanceFactory()

    def test_create_issue_with_resource(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.IssueFactory.get_list_url(), self._get_issue_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.assertEqual(self.create_issue.call_count, 1)

    def test_add_resource_info_in_description(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.IssueFactory.get_list_url(), self._get_issue_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        description_template = settings.WALDUR_JIRA['ISSUE_TEMPLATE']['RESOURCE_INFO']
        expected_description = description_template.format(resource=self.resource)
        actual_description = self.create_issue.call_args[1]['description']

        self.assertTrue(expected_description in actual_description)
        self.assertNotEqual(expected_description, actual_description)

    def test_issue_name_is_passed_to_backend(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.IssueFactory.get_list_url(), self._get_issue_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        issue_type_name = self.create_issue.call_args[1]['issuetype']['name']
        self.assertEqual(issue_type_name, self.fixture.issue_type.name)

    def test_issue_type_should_belong_to_project(self):
        self.fixture.jira_project.issue_types.remove(self.fixture.issue_type)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.IssueFactory.get_list_url(), self._get_issue_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_synchronization_resolution_sla(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.IssueFactory.get_list_url(), self._get_issue_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_issue = models.Issue.objects.get(backend_id=response.data['key'])
        self.assertEqual(new_issue.resolution_sla, self.ttr_value / 1000)

    def _get_issue_payload(self, **kwargs):
        payload = {
            'scope': structure_factories.TestNewInstanceFactory.get_url(self.resource),
        }
        payload.update(kwargs)
        return super(IssueCreateResourceTest, self)._get_issue_payload(**payload)


class IssueCreateSubtaskTest(IssueCreateBaseTest):
    def setUp(self):
        super(IssueCreateSubtaskTest, self).setUp()
        self.subtask_type = factories.IssueTypeFactory(
            subtask=True, name='Sub-task', settings=self.fixture.service_settings
        )
        self.fixture.jira_project.issue_types.add(self.subtask_type)

    def test_parent_issue_may_be_specified_for_subtask(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_issue_payload(
            parent=self.issue_url,
            type=factories.IssueTypeFactory.get_url(self.subtask_type),
        )
        response = self.client.post(factories.IssueFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.create_issue.assert_called_once_with(
            project=self.fixture.jira_project.backend_id,
            summary='Summary',
            description='description test issue',
            issuetype={'name': self.subtask_type.name},
            priority={'name': self.fixture.priority.name},
            parent={'key': self.issue.backend_id},
        )

    def test_parent_issue_valid_for_subtask_only(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_issue_payload(parent=self.issue_url,)
        response = self.client.post(factories.IssueFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_parent_issue_should_belong_to_the_same_project(self):
        self.client.force_authenticate(self.fixture.staff)

        payload = self._get_issue_payload(
            parent=factories.IssueFactory.get_url(),
            type=factories.IssueTypeFactory.get_url(self.subtask_type),
        )
        response = self.client.post(factories.IssueFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@mock.patch('waldur_jira.executors.IssueUpdateExecutor.execute')
class IssueUpdateTest(BaseTest):
    def test_author_can_update_issue(self, update_executor):
        self.client.force_authenticate(self.author)
        response = self.client.patch(self.issue_url, {'description': 'do it'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        update_executor.assert_called_once()

    def test_non_author_can_not_update_issue(self, update_executor):
        self.client.force_authenticate(self.non_author)
        response = self.client.patch(self.issue_url, {'description': 'do it'})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(update_executor.call_count, 0)


@mock.patch('waldur_jira.executors.IssueDeleteExecutor.execute')
class IssueDeleteTest(BaseTest):
    def test_author_can_delete_issue(self, delete_executor):
        self.client.force_authenticate(self.author)
        response = self.client.delete(self.issue_url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        delete_executor.assert_called_once()

    def test_non_author_can_not_delete_issue(self, delete_executor):
        self.client.force_authenticate(self.non_author)
        response = self.client.delete(self.issue_url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(delete_executor.call_count, 0)


class IssueFilterTest(BaseTest):
    def setUp(self):
        super(IssueFilterTest, self).setUp()
        self.issue_breached = factories.IssueFactory(
            project=self.fixture.jira_project,
            state=models.Issue.States.OK,
            user=self.author,
            resolution_sla=-100,
        )
        self.issue_unbreached = factories.IssueFactory(
            project=self.fixture.jira_project,
            state=models.Issue.States.OK,
            user=self.author,
            resolution_sla=100,
        )

    def test_filter_sla_ttr_breached_set_to_true(self):
        response = self._get_response(True)
        self.assertEqual(len(response.data), 1)
        self.assertTrue([issue['resolution_sla'] for issue in response.data][0] == -100)

    def test_filter_sla_ttr_breached_set_to_false(self):
        response = self._get_response(False)
        self.assertEqual(len(response.data), 1)
        self.assertTrue([issue['resolution_sla'] for issue in response.data][0] == 100)

    def test_filter_sla_ttr_breached_dont_set(self):
        response = self._get_response(None)
        self.assertEqual(len(response.data), 3)

    def _get_response(self, sla_ttr_breached):
        self.client.force_authenticate(self.fixture.staff)
        if sla_ttr_breached is not None:
            response = self.client.get(
                factories.IssueFactory.get_list_url(),
                {'sla_ttr_breached': sla_ttr_breached},  # ttr - Time to resolution
            )
        else:
            response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response
