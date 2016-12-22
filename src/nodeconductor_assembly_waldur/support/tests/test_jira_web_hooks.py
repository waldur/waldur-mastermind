import pkg_resources
import json

from django.core.urlresolvers import reverse
from jira import resources
from rest_framework import status
from rest_framework.test import APITestCase

from nodeconductor_assembly_waldur.support.tests import factories
from nodeconductor_jira.tests import factories as jira_factories


class TestJiraWebHooks(APITestCase):

    JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME = "jira_issue_updated_query.json"

    def test_issue_update_callback_updates_issue_summary(self):

        # arrange
        expected_summary = "Happy New Year"
        jira_issue = jira_factories.IssueFactory(project__backend_id="Santa")
        issue = factories.IssueFactory(backend_id=jira_issue.backend_id)
        self.assertNotEquals(issue.summary, expected_summary)

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = jira_issue.backend_id
        request_data["issue"]["fields"]["project"]["key"] = jira_issue.project.backend_id
        request_data["issue"]["fields"]["summary"] = expected_summary

        # act
        url = reverse('support-jira-webhook-list')
        response = self.client.post(url, request_data)

        self.assertEquals(response.status_code, status.HTTP_201_CREATED)
        issue.refresh_from_db()
        self.assertEqual(issue.summary, expected_summary)

    def test_issue_update_callback_updates_issue_assignee(self):

        # arrange
        jira_issue = jira_factories.IssueFactory(project__backend_id="Santa")
        issue = factories.IssueFactory(backend_id=jira_issue.backend_id)
        self.assertIsNone(issue.assignee)
        assignee = factories.SupportUserFactory()
        import pdb
        pdb.set_trace()

        jira_request = pkg_resources.resource_stream(__name__, self.JIRA_ISSUE_UPDATE_REQUEST_FILE_NAME).read().decode()
        request_data = json.loads(jira_request)
        request_data["issue"]["key"] = jira_issue.backend_id
        request_data["issue"]["fields"]["project"]["key"] = jira_issue.project.backend_id
        request_data["issue"]["fields"]["assignee"] = {
            "emailAddress": assignee.user.email
        }

        # act
        url = reverse('support-jira-webhook-list')
        response = self.client.post(url, request_data)

        self.assertEquals(response.status_code, status.HTTP_201_CREATED)
        issue.refresh_from_db()
        self.assertEqual(issue.assignee.id, assignee.id)
