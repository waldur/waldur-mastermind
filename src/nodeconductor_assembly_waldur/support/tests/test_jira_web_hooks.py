import pkg_resources
import json

from django.core.urlresolvers import reverse
from django.conf import settings
from rest_framework import status
from rest_framework.test import APITestCase

from nodeconductor_assembly_waldur.support import models
from nodeconductor_assembly_waldur.support.tests import factories


class TestJiraWebHooks(APITestCase):
    JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME = "jira_issue_updated_query.json"

    def setUp(self):
        self.url = reverse('web-hook-receiver')
        self.CREATED = 'jira:issue_created'
        self.UPDATED = 'jira:issue_updated'
        self.DELETED = 'jira:issue_deleted'

    def test_issue_update_callback_updates_issue_summary(self):
        # arrange
        expected_summary = "Happy New Year"
        backend_id = "Santa"
        issue = factories.IssueFactory(backend_id=backend_id)
        self.assertNotEquals(issue.summary, expected_summary)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        request_data["issue"]["fields"]["summary"] = expected_summary

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.summary, expected_summary)

    def test_issue_update_callback_updates_issue_assignee(self):
        # arrange
        backend_id = "Santa"
        issue = factories.IssueFactory(backend_id=backend_id)
        self.assertIsNone(issue.assignee)
        assignee = factories.SupportUserFactory(backend_id="Klaus")

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        request_data["issue"]["fields"]["assignee"] = {
            "key": assignee.backend_id
        }

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.assignee.id, assignee.id)

    def test_issue_update_callback_updates_issue_reporter(self):
        # arrange
        backend_id = "Happy New Year"
        issue = factories.IssueFactory(backend_id=backend_id)
        factories.SupportUserFactory(backend_id=backend_id)
        reporter = factories.SupportUserFactory(backend_id="Tiffany")

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        request_data["issue"]["fields"]["reporter"] = {
            "key": reporter.backend_id
        }

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.reporter.id, reporter.id)

    def test_issue_update_callback_creates_a_comment(self):
        # arrange
        backend_id = "Happy New Year"
        issue = factories.IssueFactory(backend_id=backend_id)
        factories.SupportUserFactory(backend_id=backend_id)
        self.assertEqual(issue.comments.count(), 0)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        expected_comments_count = request_data["issue"]["fields"]["comment"]["total"]

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.comments.count(), expected_comments_count)

    def test_issue_update_callback_updates_a_comment(self):
        # arrange
        backend_id = "Happy New Year"
        expected_comment_body = "Merry Christmas"
        issue = factories.IssueFactory(backend_id=backend_id)
        comment = factories.CommentFactory(issue=issue)
        factories.SupportUserFactory(backend_id=backend_id)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = issue.backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        request_data["issue"]["fields"]["comment"]["comments"][0]["id"] = comment.backend_id
        request_data["issue"]["fields"]["comment"]["comments"][0]["body"] = expected_comment_body

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        issue_comment = issue.comments.first()
        self.assertIsNotNone(issue_comment)
        self.assertEqual(issue_comment.description, expected_comment_body)

    def test_issue_update_callback_creates_deletes_two_comments(self):
        # arrange
        backend_id = "Santa"
        initial_number_of_comments = 2
        issue = factories.IssueFactory(backend_id=backend_id)
        factories.SupportUserFactory(backend_id=backend_id)
        factories.CommentFactory.create_batch(initial_number_of_comments, issue=issue)
        self.assertEqual(issue.comments.count(), initial_number_of_comments)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = issue.backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        expected_comments_count = request_data["issue"]["fields"]["comment"]["total"]

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.comments.count(), expected_comments_count)

    def test_issue_update_callback_populates_impact_field(self):

        # arrange
        impact_field = settings.WALDUR_SUPPORT["PROJECT"]["impact_field"]
        impact_field_value = 'Custom Value'
        backend_id = "Santa"
        issue = factories.IssueFactory(backend_id=backend_id)
        factories.SupportUserFactory(backend_id=backend_id)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = issue.backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = issue.reporter.backend_id
        request_data["issue"]["fields"][impact_field] = impact_field_value

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.impact, impact_field_value)

    def test_issue_update_callback_does_not_create_issue(self):
        # arrange
        backend_id = "Santa"
        reporter = factories.SupportUserFactory(backend_id=backend_id)
        self.assertEqual(models.Issue.objects.count(), 0)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["webhookEvent"] = self.CREATED
        request_data["issue"]["key"] = backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = reporter.backend_id

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        self.assertEqual(models.Issue.objects.count(), 0)

    def test_issue_update_callback_updates_issue_caller(self):
        # arrange
        expected_summary = "Happy New Year"
        backend_id = "Santa"
        issue = factories.IssueFactory(backend_id=backend_id)
        support_user = factories.SupportUserFactory(backend_id="support")

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = backend_id
        request_data["issue"]["fields"]["project"]["key"] = backend_id
        request_data["issue"]["fields"]["reporter"]["key"] = support_user.backend_id
        request_data["issue"]["fields"]["summary"] = expected_summary

        # act
        response = self.client.post(self.url, request_data)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.caller.id, support_user.user.id)
