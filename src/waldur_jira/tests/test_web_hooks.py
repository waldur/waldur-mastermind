import json
from unittest import mock

import pkg_resources
from django.urls import reverse
from rest_framework import status, test

from waldur_jira import models
from waldur_jira.tests import factories, fixtures


class BaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.JiraFixture()
        self.issue = factories.IssueFactory()
        self.url = reverse("jira-web-hook")

        self.jira_patcher = mock.patch("waldur_jira.backend.JIRA")
        self.jira_mock = self.jira_patcher.start()

        self.jira_mock().comment.return_value = mock.Mock(
            **{
                "body": "comment message",
            }
        )

    def _create_request_data(self, file_path):
        jira_request = (
            pkg_resources.resource_stream(__name__, file_path).read().decode()
        )
        self.request_data = json.loads(jira_request)
        self.request_data["issue"]["key"] = self.issue.backend_id
        self.request_data["issue"]["fields"]["project"]["key"] = (
            self.issue.project.backend_id
        )
        self.request_data["issue"]["fields"]["priority"]["id"] = (
            self.fixture.priority.backend_id
        )
        self.request_data["issue"]["fields"]["issuetype"]["id"] = (
            self.fixture.issue_type.backend_id
        )


class CommentCreateTest(BaseTest):
    JIRA_COMMENT_CREATE_REQUEST_FILE_NAME = "jira_comment_create_query.json"

    def setUp(self):
        super().setUp()
        self._create_request_data(self.JIRA_COMMENT_CREATE_REQUEST_FILE_NAME)

    def test_comment_create(self):
        result = self.client.post(self.url, self.request_data)
        self.assertEqual(result.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Comment.objects.filter(
                issue=self.issue, backend_id=result.data["comment"]["id"]
            ).exists()
        )

    def test_dont_create_comment_if_issue_not_exists(self):
        self.request_data["issue"]["key"] = ""
        result = self.client.post(self.url, self.request_data)
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)


class CommentUpdateTest(BaseTest):
    JIRA_COMMENT_UPDATE_REQUEST_FILE_NAME = "jira_comment_update_query.json"

    def setUp(self):
        super().setUp()
        self.comment = factories.CommentFactory(issue=self.issue)
        self._create_request_data(self.JIRA_COMMENT_UPDATE_REQUEST_FILE_NAME)
        self.request_data["comment"]["id"] = self.comment.backend_id

    def test_comment_update(self):
        old_message = self.comment.message
        result = self.client.post(self.url, self.request_data)
        self.assertEqual(result.status_code, status.HTTP_201_CREATED)
        self.comment.refresh_from_db()
        self.assertNotEqual(self.comment.message, old_message)

    def test_dont_update_comment_if_issue_not_exists(self):
        self.request_data["issue"]["key"] = ""
        result = self.client.post(self.url, self.request_data)
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)


class CommentDeleteTest(BaseTest):
    JIRA_COMMENT_DELETE_REQUEST_FILE_NAME = "jira_comment_delete_query.json"

    def setUp(self):
        super().setUp()
        self.comment = factories.CommentFactory(issue=self.issue)
        self._create_request_data(self.JIRA_COMMENT_DELETE_REQUEST_FILE_NAME)
        self.request_data["comment"]["id"] = self.comment.backend_id

    def test_comment_delete(self):
        self.jira_mock().comment.return_value = None
        result = self.client.post(self.url, self.request_data)
        self.assertEqual(result.status_code, status.HTTP_201_CREATED)
        self.assertFalse(
            models.Comment.objects.filter(
                backend_id=self.comment.backend_id, issue=self.issue
            ).exists()
        )

    def test_dont_delete_comment_if_exist_backend_comment(self):
        result = self.client.post(self.url, self.request_data)
        self.assertEqual(result.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Comment.objects.filter(
                backend_id=self.comment.backend_id, issue=self.issue
            ).exists()
        )
