from unittest import mock

from dbtemplates.models import Template
from rest_framework import status

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models, tasks
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.tests import factories, fixtures, smax_base
from waldur_smax.backend import Issue


class IssueCreateTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.url = factories.IssueFactory.get_list_url()
        self.fixture = fixtures.SupportFixture()
        self.caller = self.fixture.support_user.user
        self.smax_issue = Issue(1, "test", "description", "RequestStatusReady")
        self.mock_smax().add_issue.return_value = self.smax_issue

    def _get_valid_payload(self, **additional):
        payload = {
            "summary": "test_issue",
            "caller": structure_factories.UserFactory.get_url(user=self.caller),
        }
        payload.update(additional)
        return payload

    def test_create_issue(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.mock_smax().add_issue.assert_called_once()
        issue = models.Issue.objects.get(uuid=response.data["uuid"])
        self.assertEqual(str(issue.backend_id), str(self.smax_issue.id))

    def test_validate_summary_length(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url, data=self._get_valid_payload(summary="a" * 140)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        response = self.client.post(
            self.url, data=self._get_valid_payload(summary="a" * 150)
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


class SyncFromSmaxTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue = factories.IssueFactory(
            backend_name=SmaxServiceBackend.backend_name
        )
        self.smax_issue = Issue("1", "test", "description", "RequestStatusReady")
        self.mock_smax().get_issue.return_value = self.smax_issue
        self.backend = SmaxServiceBackend()

    def test_sync_issue(self):
        self.backend.sync_issues()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.summary, self.smax_issue.summary)
        self.assertEqual(self.issue.description, self.smax_issue.description)
        self.assertEqual(self.issue.status, self.smax_issue.status)

    def test_resolve_issue_from_backend(self):
        self.assertEqual(self.issue.resolved, None)
        self.smax_issue = Issue("1", "test", "description", "done")
        self.mock_smax().get_issue.return_value = self.smax_issue
        self.backend.sync_issues()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.resolved, True)

    def test_web_hook(self):
        url = "/api/support-smax-webhook/"
        response = self.client.post(url, data={"id": self.issue.backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_smax().get_issue.assert_called_once()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, self.smax_issue.status)
        self.assertEqual(self.issue.summary, self.smax_issue.summary)


class IssueLinksTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue = factories.IssueFactory(
            backend_name=SmaxServiceBackend.backend_name
        )
        self.linked_issues = [
            factories.IssueFactory(backend_name=SmaxServiceBackend.backend_name)
        ]
        self.backend = SmaxServiceBackend()

    def test_create_issue_link(self):
        self.backend.create_issue_links(self.issue, self.linked_issues)
        self.mock_smax().create_issue_link.assert_called_once()


@mock.patch("waldur_mastermind.support.tasks.core_utils.send_mail")
class IssueNotificationTest(smax_base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.SupportFixture()
        self.issue = self.fixture.issue
        self.issue.description = "<p>message</p>"
        self.issue.summary = "summary"
        self.issue.save()

    def test_update_issue_notification(self, mock_send_mail):
        Template.objects.create(
            name="support/notification_issue_updated.html",
            content="New: {{ description|safe }}, old: {{ old_description|safe }}",
        )
        Template.objects.create(
            name="support/notification_issue_updated.txt",
            content="New: {{ description }}, old: {{ old_description }}",
        )
        serialized_issue = core_utils.serialize_instance(self.issue)
        tasks.send_issue_updated_notification(
            serialized_issue, {"description": "<p>old message</p>"}
        )
        mock_send_mail.assert_called_once_with(
            f"Updated issue: {self.issue.key} {self.issue.summary}",
            "New: message\n\n, old: old message\n\n",
            [self.issue.caller.email],
            html_message="New: <p>message</p>, old: <p>old message</p>",
        )
