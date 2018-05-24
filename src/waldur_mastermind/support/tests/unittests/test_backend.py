from __future__ import unicode_literals

from django.test import TestCase
import mock

from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.tests import fixtures


class BackendTest(TestCase):

    def setUp(self):
        super(BackendTest, self).setUp()

        self.fixture = fixtures.SupportFixture()
        self.backend = ServiceDeskBackend()

        jira_patcher = mock.patch('waldur_jira.backend.JIRA')
        self.mocked_jira = jira_patcher.start()()

        self.mocked_jira.fields.return_value = [
            {
                'clauseNames': 'Caller',
                'id': 'field101',
            },
            {
                'clauseNames': 'Original Reporter',
                'id': 'field102',
            },
            {
                'clauseNames': 'Time to first response',
                'id': 'field103',
            },
            {
                'clauseNames': 'Impact',
                'id': 'field104',
            },
        ]

    def tearDown(self):
        super(BackendTest, self).tearDown()
        mock.patch.stopall()

    def test_user_is_created_for_issue(self):
        # Arrange
        issue = self.fixture.issue
        issue.type = 'Task'
        issue.priority = 'Major'
        issue.save()

        self.mocked_jira.create_issue.return_value = mock.Mock(**{
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
            'fields.field104': 'Critical'  # Impact
        })
        self.mocked_jira.create_issue.return_value.permalink.return_value = 'http://example.com/TST-101'

        # Act
        self.backend.create_issue(issue)

        # Assert
        self.mocked_jira.add_user.assert_called_once_with(
            issue.caller.email, issue.caller.email,
            fullname=issue.caller.full_name, ignore_existing=True
        )
        self.mocked_jira.create_issue.assert_called_once_with(
            project='PROJECT',
            summary=issue.summary,
            description=issue.description,
            issuetype={'name': issue.type},
            priority={'name': issue.priority},
            field101=[
                {
                    'name': issue.caller.email,
                    'key': issue.caller.email
                }
            ],
            field102=issue.reporter.name,
        )
