import base64
import collections
import json
from io import BytesIO
from unittest import mock

import jira
from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITransactionTestCase

from waldur_jira.backend import AttachmentSynchronizer, CommentSynchronizer
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.tests import factories
from waldur_mastermind.support.tests.base import load_resource
from waldur_mastermind.support.tests.utils import override_plugin_settings


@mock.patch('waldur_mastermind.support.serializers.ServiceDeskBackend')
@override_plugin_settings(
    ENABLED=True,
    ACTIVE_BACKEND_TYPE='basic',
)
@override_settings(task_always_eager=True)
class TestJiraWebHooks(APITransactionTestCase):
    def setUp(self):
        self.url = reverse('web-hook-receiver')
        backend_id = 'SNT-101'
        self.issue = factories.IssueFactory(backend_id=backend_id)

        def create_request(test, name, path):
            jira_request = json.loads(load_resource(path))
            jira_request['issue']['key'] = backend_id
            setattr(test, 'request_data_' + name, jira_request)

        jira_requests = (
            ('issue_updated', 'jira_issue_updated_query.json'),
            ('comment_create', 'jira_comment_create_query.json'),
            ('comment_update', 'jira_comment_update_query.json'),
            ('comment_delete', 'jira_comment_delete_query.json'),
        )
        [create_request(self, *r) for r in jira_requests]

    def test_issue_update(self, mock_jira):
        self.request_data_issue_updated['issue_event_type_name'] = 'issue_updated'
        self.client.post(self.url, self.request_data_issue_updated)
        self.assertTrue(self._call_update_issue(mock_jira))

    def test_generic_update(self, mock_jira):
        self.request_data_issue_updated['issue_event_type_name'] = 'issue_generic'
        self.client.post(self.url, self.request_data_issue_updated)
        self.assertTrue(self._call_update_issue(mock_jira))

    def test_comment_create(self, mock_jira):
        self.client.post(self.url, self.request_data_comment_create)
        self.assertTrue(self._call_create_comment(mock_jira))

    def test_comment_update(self, mock_jira):
        comment = factories.CommentFactory(issue=self.issue)
        self.request_data_comment_update['comment']['id'] = comment.backend_id
        self.client.post(self.url, self.request_data_comment_update)
        self.assertTrue(self._call_update_comment(mock_jira))

    def test_comment_delete(self, mock_jira):
        comment = factories.CommentFactory(issue=self.issue)
        self.request_data_comment_delete['comment']['id'] = comment.backend_id
        self.client.post(self.url, self.request_data_comment_delete)
        self.assertTrue(self._call_delete_comment(mock_jira))

    def test_add_attachment(self, mock_jira):
        self.request_data_issue_updated['issue_event_type_name'] = 'issue_updated'
        self.client.post(self.url, self.request_data_issue_updated)
        self.assertTrue(self._call_update_attachment(mock_jira))

    def test_delete_attachment(self, mock_jira):
        self.request_data_issue_updated['issue_event_type_name'] = 'issue_updated'
        self.client.post(self.url, self.request_data_issue_updated)
        self.assertTrue(self._call_update_attachment(mock_jira))

    def _call_update_attachment(self, mock_jira):
        return filter(
            lambda x: x[0] == '().update_attachment_from_jira', mock_jira.mock_calls
        )

    def _call_create_comment(self, mock_jira):
        return filter(
            lambda x: x[0] == '().create_comment_from_jira', mock_jira.mock_calls
        )

    def _call_update_comment(self, mock_jira):
        return filter(
            lambda x: x[0] == '().update_comment_from_jira', mock_jira.mock_calls
        )

    def _call_delete_comment(self, mock_jira):
        return filter(
            lambda x: x[0] == '().delete_comment_from_jira', mock_jira.mock_calls
        )

    def _call_update_issue(self, mock_jira):
        return filter(
            lambda x: x[0] == '().update_issue_from_jira', mock_jira.mock_calls
        )


MockSupportUser = collections.namedtuple('MockSupportUser', ['key'])
MockResolution = collections.namedtuple('MockResolution', ['name'])


@override_settings(task_always_eager=True)
@override_plugin_settings(ENABLED=True)
class TestUpdateIssueFromJira(APITransactionTestCase):
    def setUp(self):
        self.issue = factories.IssueFactory()

        backend_issue_raw = json.loads(load_resource('jira_issue_raw.json'))
        self.backend_issue = jira.resources.Issue(
            {'server': 'example.com'}, None, backend_issue_raw
        )

        self.impact_field_id = 'customfield_10116'
        self.request_feedback = 'customfield_10216'
        self.first_response_sla = timezone.now()

        def side_effect(arg):
            if arg == 'Impact':
                return self.impact_field_id
            elif arg == 'Request feedback':
                return self.request_feedback

        self.backend = ServiceDeskBackend()
        self.backend.get_backend_issue = mock.Mock(return_value=self.backend_issue)
        self.backend._get_first_sla_field = mock.Mock(
            return_value=self.first_response_sla
        )
        self.backend.get_field_id_by_name = mock.Mock(side_effect=side_effect)

    def update_issue_from_jira(self):
        self.backend.update_issue_from_jira(self.issue)
        self.issue.refresh_from_db()

    def test_update_issue_impact_field(self):
        impact_field_value = 'Custom Value'
        setattr(self.backend_issue.fields, self.impact_field_id, impact_field_value)
        self.update_issue_from_jira()
        self.assertEqual(self.issue.impact, impact_field_value)

    def test_update_issue_assignee(self):
        assignee = factories.SupportUserFactory(backend_id='support_user_backend_id')
        backend_assignee_user = MockSupportUser(key=assignee.backend_id)
        self.backend_issue.fields.assignee = backend_assignee_user
        self.update_issue_from_jira()
        self.assertEqual(self.issue.assignee.id, assignee.id)

    def test_update_issue_reporter(self):
        reporter = factories.SupportUserFactory(backend_id='support_user_backend_id')
        backend_reporter_user = MockSupportUser(key=reporter.backend_id)
        self.backend_issue.fields.reporter = backend_reporter_user
        self.update_issue_from_jira()
        self.assertEqual(self.issue.reporter.id, reporter.id)

    def test_update_issue_summary(self):
        expected_summary = 'Happy New Year'
        self.backend_issue.fields.summary = expected_summary
        self.update_issue_from_jira()
        self.assertEqual(self.issue.summary, expected_summary)

    def test_update_issue_link(self):
        permalink = self.backend_issue.permalink()
        self.update_issue_from_jira()
        self.assertEqual(self.issue.link, permalink)

    def test_update_first_response_sla(self):
        self.update_issue_from_jira()
        self.assertEqual(self.issue.first_response_sla, self.first_response_sla)

    def test_update_issue_resolution(self):
        expected_resolution = MockResolution(name='Done')
        self.backend_issue.fields.resolution = expected_resolution
        self.update_issue_from_jira()
        self.assertEqual(self.issue.resolution, expected_resolution.name)

    def test_resolution_is_empty_if_it_is_none(self):
        expected_resolution = None
        self.backend_issue.fields.resolution = expected_resolution
        self.update_issue_from_jira()
        self.assertEqual(self.issue.resolution, '')

    def test_update_issue_status(self):
        self.update_issue_from_jira()
        self.assertEqual(self.issue.status, self.backend_issue.fields.status.name)

    def test_web_hook_does_not_trigger_issue_update_email_if_the_issue_was_not_updated(
        self,
    ):
        self.update_issue_from_jira()
        self.update_issue_from_jira()
        self.assertEqual(len(mail.outbox), 0)

    def test_web_hook_does_trigger_issue_update_email_if_the_issue_was_updated(self):
        self.update_issue_from_jira()
        self.backend_issue.fields.summary = 'New summary'
        self.update_issue_from_jira()
        self.assertEqual(len(mail.outbox), 1)

    def test_issue_update_callback_creates_deletes_two_comments(self):
        factories.CommentFactory(issue=self.issue)
        factories.CommentFactory(issue=self.issue)
        synchronizer = CommentSynchronizer(self.backend, self.issue, self.backend_issue)
        synchronizer.perform_update()
        self.assertEqual(self.issue.comments.count(), 0)

    def test_update_issue_feedback_request_field(self):
        self.update_issue_from_jira()
        self.assertEqual(self.issue.feedback_request, True)

        setattr(self.backend_issue.fields, self.request_feedback, None)
        self.update_issue_from_jira()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.feedback_request, False)


class TestUpdateCommentFromJira(APITransactionTestCase):
    def setUp(self):
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND_TYPE'] = SupportBackendType.ATLASSIAN
        self.comment = factories.CommentFactory()

        backend_comment_raw = json.loads(load_resource('jira_comment_raw.json'))
        self.backend_comment = jira.resources.Comment(
            {'server': 'example.com'}, None, backend_comment_raw
        )
        self.backend = ServiceDeskBackend()

        self.internal = {'value': {'internal': False}}
        path = mock.patch.object(
            ServiceDeskBackend,
            '_get_property',
            new=mock.Mock(return_value=self.internal),
        )
        path.start()

        path = mock.patch.object(
            ServiceDeskBackend,
            'get_backend_comment',
            new=mock.Mock(return_value=self.backend_comment),
        )
        path.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_update_comment_description(self):
        self.backend.update_comment_from_jira(self.comment)
        self.comment.refresh_from_db()
        self.assertEqual(
            self.comment.description,
            self.comment.clean_message(self.backend_comment.body),
        )

    def test_update_comment_is_public(self):
        self.internal['value']['internal'] = True
        self.backend.update_comment_from_jira(self.comment)
        self.internal['value']['internal'] = False
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.is_public, False)

    def test_webhook_cleans_up_user_info_and_does_not_update_comment_if_it_is_not_changed(
        self,
    ):
        expected_comment_body = self.comment.description
        jira_comment_body = '[Luke Skywalker 19BBY-TA-T16]: %s' % expected_comment_body
        self.backend_comment.body = jira_comment_body
        self.backend.update_comment_from_jira(self.comment)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.description, expected_comment_body)


class TestUpdateAttachmentFromJira(APITransactionTestCase):
    def setUp(self):
        settings.WALDUR_SUPPORT['ENABLED'] = True
        settings.WALDUR_SUPPORT['ACTIVE_BACKEND_TYPE'] = SupportBackendType.ATLASSIAN
        self.issue = factories.IssueFactory()

        backend_issue_raw = json.loads(load_resource('jira_issue_raw.json'))
        self.backend_issue = jira.resources.Issue(
            {'server': 'example.com'}, None, backend_issue_raw
        )

        backend_attachment_raw = json.loads(load_resource('jira_attachment_raw.json'))
        self.backend_attachment = jira.resources.Attachment(
            {'server': 'example.com'}, None, backend_attachment_raw
        )
        self.backend_issue.fields.attachment.append(self.backend_attachment)

        self.backend = ServiceDeskBackend()

        path = mock.patch.object(
            ServiceDeskBackend,
            'get_backend_issue',
            new=mock.Mock(return_value=self.backend_issue),
        )
        path.start()

        path = mock.patch.object(
            ServiceDeskBackend,
            'get_backend_attachment',
            new=mock.Mock(return_value=self.backend_attachment),
        )
        path.start()

        file_content = BytesIO(
            base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        )
        path = mock.patch.object(
            AttachmentSynchronizer,
            '_download_file',
            new=mock.Mock(return_value=file_content),
        )
        path.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_add_attachment(self):
        self.backend.update_attachment_from_jira(self.issue)
        self.assertEqual(self.issue.attachments.count(), 1)

    def test_delete_attachment(self):
        self.backend.update_attachment_from_jira(self.issue)
        self.assertEqual(self.issue.attachments.count(), 1)
        self.backend_issue.fields.attachment = []
        self.backend.update_attachment_from_jira(self.issue)
        self.assertEqual(self.issue.attachments.count(), 0)
