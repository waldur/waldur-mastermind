import json
from unittest import mock

import jira
from django.test import TestCase
from django.utils import timezone
from jira import User

from waldur_core.core.utils import datetime_to_timestamp
from waldur_mastermind.support import models
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.tests import factories, fixtures
from waldur_mastermind.support.tests.base import load_resource


class BaseBackendTest(TestCase):
    def setUp(self):
        super(BaseBackendTest, self).setUp()

        self.fixture = fixtures.SupportFixture()
        self.backend = ServiceDeskBackend()

        jira_patcher = mock.patch('waldur_jira.backend.JIRA')
        self.mocked_jira = jira_patcher.start()()

        self.mocked_jira.fields.return_value = json.loads(
            load_resource('jira_fields.json')
        )

        mock_backend_users = [
            User({'server': ''}, None, raw={'key': 'user_1', 'active': True})
        ]
        self.mocked_jira.waldur_search_users.return_value = mock_backend_users

    def tearDown(self):
        super(BaseBackendTest, self).tearDown()
        mock.patch.stopall()


class IssueCreateTest(BaseBackendTest):
    def setUp(self):
        super(IssueCreateTest, self).setUp()
        issue = self.fixture.issue
        issue.type = 'Task'
        issue.priority = 'Major'
        issue.save()
        self.issue = issue
        factories.RequestTypeFactory(issue_type_name=issue.type)

        self.mocked_jira.create_customer_request.return_value = mock.Mock(
            **{
                'key': 'TST-101',
                'fields.assignee.key': '',
                'fields.assignee.name': '',
                'fields.assignee.emailAddress': '',
                'fields.assignee.displayName': '',
                'fields.creator.key': '',
                'fields.creator.name': '',
                'fields.creator.emailAddress': '',
                'fields.creator.displayName': '',
                'fields.reporter.key': '',
                'fields.reporter.name': '',
                'fields.reporter.emailAddress': '',
                'fields.reporter.displayName': '',
                'fields.resolutiondate': '',
                'fields.summary': '',
                'fields.description': '',
                'fields.status.name': '',
                'fields.resolution': '',
                'fields.priority.name': 'Major',
                'fields.issuetype.name': 'Task',
                'fields.field103.ongoingCycle.breachTime.epochMillis': 1000,  # SLA
                'fields.field104': 'Critical',  # Impact
                'permalink()': '',
            }
        )
        self.mocked_jira.create_customer_request.return_value.permalink.return_value = (
            'http://example.com/TST-101'
        )

    def test_user_for_caller_is_created(self):
        self.mocked_jira.waldur_search_users.return_value = []
        self.backend.create_issue(self.issue)
        self.mocked_jira.create_customer.assert_called_once_with(
            self.issue.caller.email, self.issue.caller.full_name
        )

    def test_caller_is_specified_in_custom_field(self):
        self.backend.create_issue(self.issue)

        kwargs = self.mocked_jira.create_customer_request.call_args[0][0]
        self.assertEqual(
            kwargs['requestParticipants'],
            [self.issue.caller.supportcustomer.backend_id],
        )

    def test_original_reporter_is_specified_in_custom_field(self):
        self.backend.create_issue(self.issue)
        kwargs = self.mocked_jira.create_customer_request.return_value.update.call_args[
            1
        ]
        self.assertEqual(kwargs['field102'], self.issue.reporter.name)


class IssueUpdateTest(BaseBackendTest):
    def setUp(self):
        super(IssueUpdateTest, self).setUp()
        self.mocked_jira.issue.return_value = mock.Mock(
            **{
                'key': 'TST-101',
                'fields.assignee.key': '',
                'fields.assignee.name': '',
                'fields.assignee.emailAddress': '',
                'fields.assignee.displayName': '',
                'fields.creator.key': '',
                'fields.creator.name': '',
                'fields.creator.emailAddress': '',
                'fields.creator.displayName': '',
                'fields.reporter.key': '',
                'fields.reporter.name': '',
                'fields.reporter.emailAddress': '',
                'fields.reporter.displayName': '',
                'fields.resolutiondate': '',
                'fields.summary': '',
                'fields.description': '',
                'fields.status.name': '',
                'fields.resolution': '',
                'fields.priority.name': 'Major',
                'fields.issuetype.name': 'Task',
                'fields.field103.ongoingCycle.breachTime.epochMillis': 1000,  # SLA
                'fields.field104': 'Critical',  # Impact
            }
        )
        self.mocked_jira.issue.return_value.permalink.return_value = (
            'http://example.com/TST-101'
        )

    def test_sla_is_populated(self):
        # Arrange
        issue = self.fixture.issue
        dt = timezone.now().replace(microsecond=0)
        ts = datetime_to_timestamp(dt) * 1000
        self.mocked_jira.issue.return_value.fields.field103.ongoingCycle.breachTime.epochMillis = (
            ts
        )

        # Act
        self.backend.update_issue_from_jira(issue)
        issue.refresh_from_db()

        # Assert
        self.assertEqual(issue.first_response_sla, dt)

    def test_assignee_is_populated(self):
        issue = self.fixture.issue
        self.mocked_jira.issue.return_value.fields.assignee.key = 'alice@lebowski.com'
        self.backend.update_issue_from_jira(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.assignee.backend_id, 'alice@lebowski.com')

    def test_reporter_is_populated(self):
        issue = self.fixture.issue
        self.mocked_jira.issue.return_value.fields.reporter.key = 'bob@lebowski.com'
        self.backend.update_issue_from_jira(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.reporter.backend_id, 'bob@lebowski.com')

    def test_issue_is_resolved(self):
        issue = self.fixture.issue
        resolution_date = timezone.now()
        self.mocked_jira.issue.return_value.fields.status.name = 'Resolved'
        self.mocked_jira.issue.return_value.fields.resolutiondate = resolution_date

        self.backend.update_issue_from_jira(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.resolution_date, resolution_date)


class CommentCreateTest(BaseBackendTest):
    def setUp(self):
        super(CommentCreateTest, self).setUp()
        self.comment = self.fixture.comment

        class Response:
            status_code = 201

            def json(self):
                return {'id': '10001'}

        self.mocked_jira._session.post.return_value = Response()

    def create_comment(self):
        self.backend.create_comment(self.comment)
        kwargs = self.mocked_jira._session.post.call_args[1]
        data = json.loads(kwargs['data'])
        return data

    def test_backend_id_is_populated(self):
        self.create_comment()
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.backend_id, '10001')

    def test_original_author_is_specified(self):
        self.comment.description = 'Comment description'
        self.comment.save()

        user = self.comment.author.user
        user.full_name = 'Alice Lebowski'
        user.civil_number = None
        user.save()

        data = self.create_comment()
        self.assertEqual('[Alice Lebowski]: Comment description', data['body'])

    def test_internal_flag_is_specified(self):
        self.comment.is_public = False
        self.comment.save()

        data = self.create_comment()
        expected = [{'key': 'sd.public.comment', 'value': {'internal': True}}]
        self.assertEqual(expected, data['properties'])

    def test_of_author_when_create_comment_from_jira(self):
        issue = factories.IssueFactory()
        backend_comment_raw = json.loads(load_resource('jira_comment_raw.json'))
        self.backend_comment = jira.resources.Comment(
            {'server': 'example.com'}, None, backend_comment_raw
        )
        self.mocked_jira.comment.return_value = self.backend_comment
        self.backend.create_comment_from_jira(issue, self.backend_comment.id)
        comment = models.Comment.objects.get(issue=issue)
        self.assertEqual(comment.author.backend_id, 'user')


class CommentUpdateTest(BaseBackendTest):
    def setUp(self):
        super(CommentUpdateTest, self).setUp()
        self.mocked_jira.comment.return_value = mock.Mock(
            **{
                'body': '[Alice Lebowski]: New comment description',
                'author': mock.Mock(**{'key': 'alice@lebowski.com'}),
            }
        )
        self.mocked_jira._session.get.return_value.json.return_value = {
            'value': {'internal': True}
        }

    def test_description_is_updated(self):
        # Arrange
        comment = self.fixture.comment
        comment.description = 'Old comment description'
        comment.save()

        # Act
        self.backend.update_comment_from_jira(comment)

        # Assert
        comment.refresh_from_db()
        self.assertEqual(comment.description, 'New comment description')

    def test_author_is_populated(self):
        comment = self.fixture.comment
        self.backend.update_comment_from_jira(comment)
        comment.refresh_from_db()

        self.assertEqual(comment.author.backend_id, 'alice@lebowski.com')

    def test_internal_flag_is_updated(self):
        # Arrange
        comment = self.fixture.comment
        comment.is_public = True
        comment.save()

        # Act
        self.backend.update_comment_from_jira(comment)

        # Assert
        comment.refresh_from_db()
        self.assertFalse(comment.is_public)
