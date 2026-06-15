import base64
import collections
import unittest
from io import BytesIO
from unittest import mock

# Remove jira import - using atlassian-python-api instead
from constance.test.unittest import override_config as override_constance_config
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from waldur_core.core.tests.helpers import load_json_resource
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.atlassian import (
    AttachmentSynchronizer,
    CommentSynchronizer,
    ServiceDeskBackend,
)
from waldur_mastermind.support.tests import factories

JIRA_WEBHOOK_TEST_SECRET = "jira-test-secret"  # noqa: S105


@mock.patch("waldur_mastermind.support.serializers.ServiceDeskBackend")
@override_constance_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    JIRA_WEBHOOK_SHARED_SECRET=JIRA_WEBHOOK_TEST_SECRET,
)
@override_settings(task_always_eager=True)
class TestJiraWebHooks(APITestCase):
    def setUp(self):
        self.url = reverse("web-hook-receiver")
        backend_id = "SNT-101"
        self.issue = factories.IssueFactory(backend_id=backend_id)

        def create_request(test, name, path):
            jira_request = load_json_resource(path, __name__)
            jira_request["issue"]["key"] = backend_id
            setattr(test, "request_data_" + name, jira_request)

        jira_requests = (
            ("issue_updated", "jira_issue_updated_query.json"),
            ("comment_create", "jira_comment_create_query.json"),
            ("comment_update", "jira_comment_update_query.json"),
            ("comment_delete", "jira_comment_delete_query.json"),
        )
        [create_request(self, *r) for r in jira_requests]

    def _post(self, body):
        # Inbound webhooks now require the X-Webhook-Secret header — see SEC-C7.
        return self.client.post(
            self.url, body, HTTP_X_WEBHOOK_SECRET=JIRA_WEBHOOK_TEST_SECRET
        )

    def test_issue_update(self, mock_jira):
        self.request_data_issue_updated["issue_event_type_name"] = "issue_updated"
        self._post(self.request_data_issue_updated)
        self.assertTrue(self._call_update_issue(mock_jira))

    def test_generic_update(self, mock_jira):
        self.request_data_issue_updated["issue_event_type_name"] = "issue_generic"
        self._post(self.request_data_issue_updated)
        self.assertTrue(self._call_update_issue(mock_jira))

    def test_comment_create(self, mock_jira):
        self._post(self.request_data_comment_create)
        self.assertTrue(self._call_create_comment(mock_jira))

    def test_comment_update(self, mock_jira):
        comment = factories.CommentFactory(issue=self.issue)
        self.request_data_comment_update["comment"]["id"] = comment.backend_id
        self._post(self.request_data_comment_update)
        self.assertTrue(self._call_update_comment(mock_jira))

    def test_comment_delete(self, mock_jira):
        comment = factories.CommentFactory(issue=self.issue)
        self.request_data_comment_delete["comment"]["id"] = comment.backend_id
        self._post(self.request_data_comment_delete)
        self.assertTrue(self._call_delete_comment(mock_jira))

    def test_add_attachment(self, mock_jira):
        self.request_data_issue_updated["issue_event_type_name"] = "issue_updated"
        self._post(self.request_data_issue_updated)
        self.assertTrue(self._call_update_attachment(mock_jira))

    def test_delete_attachment(self, mock_jira):
        self.request_data_issue_updated["issue_event_type_name"] = "issue_updated"
        self._post(self.request_data_issue_updated)
        self.assertTrue(self._call_update_attachment(mock_jira))

    def _call_update_attachment(self, mock_jira):
        return filter(
            lambda x: x[0] == "().update_attachment_from_jira", mock_jira.mock_calls
        )

    def _call_create_comment(self, mock_jira):
        return filter(
            lambda x: x[0] == "().create_comment_from_jira", mock_jira.mock_calls
        )

    def _call_update_comment(self, mock_jira):
        return filter(
            lambda x: x[0] == "().update_comment_from_jira", mock_jira.mock_calls
        )

    def _call_delete_comment(self, mock_jira):
        return filter(
            lambda x: x[0] == "().delete_comment_from_jira", mock_jira.mock_calls
        )

    def _call_update_issue(self, mock_jira):
        return filter(
            lambda x: x[0] == "().update_issue_from_jira", mock_jira.mock_calls
        )


MockSupportUser = collections.namedtuple("MockSupportUser", ["key"])
MockResolution = collections.namedtuple("MockResolution", ["name"])


@override_settings(task_always_eager=True)
@override_constance_config(WALDUR_SUPPORT_ENABLED=True)
class TestUpdateIssueFromJira(APITestCase):
    def setUp(self):
        self.issue = factories.IssueFactory()

        # Mock backend issue using the updated Service Desk API format
        raw_data = load_json_resource("service_desk_issue_raw.json", __name__)
        self.backend_issue = mock.MagicMock()
        self.backend_issue.raw = raw_data

        # Service Desk API format compatibility
        self.backend_issue.issueKey = raw_data.get("issueKey")
        self.backend_issue.issueId = raw_data.get("issueId")
        self.backend_issue.currentStatus = raw_data.get("currentStatus", {})

        # For backwards compatibility, also populate fields from requestFieldValues
        self.backend_issue.fields = mock.MagicMock()
        # Convert requestFieldValues array to fields object for compatibility
        fields_dict = {}
        for field_value in raw_data.get("requestFieldValues", []):
            field_id = field_value.get("fieldId")
            if field_id:
                fields_dict[field_id] = field_value.get("value")

        # Add any SLA fields
        sla_data = raw_data.get("sla", {})
        for sla_field, sla_value in sla_data.items():
            fields_dict[sla_field] = sla_value

        for field, value in fields_dict.items():
            setattr(self.backend_issue.fields, field, value)

        self.impact_field_id = "customfield_10116"
        self.request_feedback = "customfield_10216"

        # Also create dictionary representation for Service Desk API format
        self.backend_issue_dict = {
            "issueKey": "TST-16",
            "issueId": raw_data.get("id", "12345"),
            "requestFieldValues": [
                {"fieldId": self.request_feedback, "value": True}  # Add feedback field
            ],
            "currentStatus": {
                "status": raw_data.get("fields", {})
                .get("status", {})
                .get("name", "Open")
            },
            "summary": raw_data.get("fields", {}).get("summary", "Test summary"),
            "_links": {"agent": "https://example.com/browse/TST-16"},
        }
        self.backend_issue.__getitem__ = lambda self, key: self.backend_issue_dict[key]
        self.backend_issue.get = lambda key, default=None: self.backend_issue_dict.get(
            key, default
        )
        # Add permalink method for compatibility
        self.backend_issue.permalink = lambda: self.backend_issue_dict["_links"][
            "agent"
        ]

        # Helper method to sync JIRA fields to Service Desk format
        def sync_field_to_service_desk(field_id, value):
            for field in self.backend_issue_dict["requestFieldValues"]:
                if field["fieldId"] == field_id:
                    field["value"] = value
                    break
            else:
                self.backend_issue_dict["requestFieldValues"].append(
                    {"fieldId": field_id, "value": value}
                )

        self.sync_field_to_service_desk = sync_field_to_service_desk

        def side_effect(arg):
            if arg == "Impact":
                return self.impact_field_id
            elif arg == "Request feedback":
                return self.request_feedback

        self.backend = ServiceDeskBackend()
        # Mock the manager to avoid actual API calls
        self.backend.manager = mock.MagicMock()
        self.backend.manager.get_customer_request.return_value = self.backend_issue_dict
        # Mock the get method for resolution lookup
        self.backend.manager.get.return_value = {"fields": {"resolution": None}}

        self.backend.get_backend_issue = mock.Mock(return_value=self.backend_issue)
        self.backend.get_field_id_by_name = mock.Mock(side_effect=side_effect)

    def update_issue_from_jira(self):
        self.backend.update_issue_from_jira(self.issue)
        self.issue.refresh_from_db()

    def test_update_issue_impact_field(self):
        impact_field_value = "Custom Value"
        setattr(self.backend_issue.fields, self.impact_field_id, impact_field_value)
        # Sync the JIRA field to Service Desk format
        self.sync_field_to_service_desk(self.impact_field_id, impact_field_value)
        self.update_issue_from_jira()
        self.assertEqual(self.issue.impact, impact_field_value)

    @unittest.skip
    def test_update_issue_assignee(self):
        assignee = factories.SupportUserFactory(backend_id="support_user_backend_id")
        backend_assignee_user = MockSupportUser(key=assignee.backend_id)
        self.backend_issue.fields.assignee = backend_assignee_user
        self.update_issue_from_jira()
        self.assertEqual(self.issue.assignee.id, assignee.id)

    @unittest.skip
    def test_update_issue_reporter(self):
        reporter = factories.SupportUserFactory(backend_id="support_user_backend_id")
        backend_reporter_user = MockSupportUser(key=reporter.backend_id)
        self.backend_issue.fields.reporter = backend_reporter_user
        self.update_issue_from_jira()
        self.assertEqual(self.issue.reporter.id, reporter.id)

    def test_update_issue_summary(self):
        expected_summary = "Happy New Year"
        self.backend_issue.fields.summary = expected_summary
        # Also update the dictionary format for Service Desk API
        self.backend_issue_dict["summary"] = expected_summary
        self.update_issue_from_jira()
        self.assertEqual(self.issue.summary, expected_summary)

    def test_update_issue_link(self):
        permalink = self.backend_issue.permalink()
        self.update_issue_from_jira()
        self.assertEqual(self.issue.link, permalink)

    def test_update_issue_resolution(self):
        expected_resolution = MockResolution(name="Done")
        self.backend_issue.fields.resolution = expected_resolution
        # Update the manager mock to return resolution
        self.backend.manager.get.return_value = {
            "fields": {"resolution": {"name": expected_resolution.name}}
        }
        # Update the dictionary to have the correct status
        self.backend_issue_dict["currentStatus"]["status"] = expected_resolution.name
        self.update_issue_from_jira()
        self.assertEqual(self.issue.resolution, expected_resolution.name)

    def test_resolution_is_empty_if_it_is_none(self):
        expected_resolution = None
        self.backend_issue.fields.resolution = expected_resolution
        self.update_issue_from_jira()
        self.assertEqual(self.issue.resolution, "")

    def test_update_issue_status(self):
        # The status field gets the currentStatus.status from Service Desk API
        self.update_issue_from_jira()
        # The status comes from currentStatus.status in the backend_issue_dict,
        # which defaults to "Open" in our test setup
        self.assertEqual(self.issue.status, "Open")

    def test_web_hook_does_not_trigger_issue_update_email_if_the_issue_was_not_updated(
        self,
    ):
        self.update_issue_from_jira()
        self.update_issue_from_jira()
        self.assertEqual(len(mail.outbox), 0)

    @unittest.skip
    def test_web_hook_does_trigger_issue_update_email_if_the_issue_was_updated(self):
        self.update_issue_from_jira()
        self.backend_issue.fields.summary = "New summary"
        self.update_issue_from_jira()
        self.assertEqual(len(mail.outbox), 1)

    def test_issue_update_callback_creates_deletes_two_comments(self):
        factories.CommentFactory(issue=self.issue)
        factories.CommentFactory(issue=self.issue)
        synchronizer = CommentSynchronizer(self.backend, self.issue, self.backend_issue)
        synchronizer.delete_old_comments()
        self.assertEqual(self.issue.comments.count(), 0)

    def test_update_issue_feedback_request_field(self):
        self.update_issue_from_jira()
        self.assertEqual(self.issue.feedback_request, True)

        # Update the request field values to have no value
        self.backend_issue_dict["requestFieldValues"] = [
            {"fieldId": self.request_feedback, "value": None}
        ]
        setattr(self.backend_issue.fields, self.request_feedback, None)
        self.update_issue_from_jira()
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.feedback_request, False)


class TestUpdateCommentFromJira(APITestCase):
    @override_constance_config(
        WALDUR_SUPPORT_ENABLED=True,
        WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ATLASSIAN,
    )
    def setUp(self):
        self.comment = factories.CommentFactory()

        # Mock backend comment using the updated Service Desk API format
        raw_data = load_json_resource("service_desk_comment_raw.json", __name__)
        # Use Service Desk API format directly
        self.service_desk_comment = {
            "id": raw_data["id"],
            "body": raw_data["body"],
            "author": raw_data["author"],
            "public": raw_data.get("public", True),
            "created": raw_data.get("created", {}),
            "_links": raw_data.get("_links", {}),
        }
        self.backend_comment = mock.MagicMock()
        self.backend_comment.raw = raw_data
        for field, value in raw_data.items():
            setattr(self.backend_comment, field, value)

        # Make the mock work with dictionary access as well for Service Desk API
        self.backend_comment.__getitem__ = lambda _, key: self.service_desk_comment[key]
        self.backend_comment.get = lambda _, key, default=None: (
            self.service_desk_comment.get(key, default)
        )

        # Helper method to sync changes between attribute and dictionary access
        def sync_body_to_dict():
            if hasattr(self.backend_comment, "body"):
                self.service_desk_comment["body"] = self.backend_comment.body

        self.sync_body_to_dict = sync_body_to_dict
        self.backend = ServiceDeskBackend()

        self.internal = {"value": {"internal": False}}
        path = mock.patch.object(
            ServiceDeskBackend,
            "_get_property",
            new=mock.Mock(return_value=self.internal),
        )
        path.start()

        path = mock.patch.object(
            ServiceDeskBackend,
            "get_backend_comment",
            new=mock.Mock(return_value=self.service_desk_comment),
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
        # Update the Service Desk API format to set comment as internal (public=False)
        self.service_desk_comment["public"] = False
        self.backend.update_comment_from_jira(self.comment)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.is_public, False)

    def test_update_comment_is_public_via_jsd_public(self):
        # REST API v2/v3 uses "jsdPublic" instead of "public"
        del self.service_desk_comment["public"]
        self.service_desk_comment["jsdPublic"] = False
        self.backend.update_comment_from_jira(self.comment)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.is_public, False)

    def test_update_comment_defaults_to_public_when_no_flag(self):
        # When neither "public" nor "jsdPublic" is present, default to public
        self.service_desk_comment.pop("public", None)
        self.service_desk_comment.pop("jsdPublic", None)
        self.backend.update_comment_from_jira(self.comment)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.is_public, True)

    def test_webhook_cleans_up_user_info_and_does_not_update_comment_if_it_is_not_changed(
        self,
    ):
        expected_comment_body = self.comment.description
        jira_comment_body = "[Luke Skywalker 19BBY-TA-T16]: %s" % expected_comment_body
        self.backend_comment.body = jira_comment_body
        self.sync_body_to_dict()  # Sync the attribute change to dictionary access
        self.backend.update_comment_from_jira(self.comment)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.description, expected_comment_body)


class TestUpdateAttachmentFromJira(APITestCase):
    @override_constance_config(
        WALDUR_SUPPORT_ENABLED=True,
        WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ATLASSIAN,
    )
    def setUp(self):
        self.issue = factories.IssueFactory()

        # Mock backend issue using the updated Service Desk API format
        raw_data = load_json_resource("service_desk_issue_raw.json", __name__)
        self.backend_issue = mock.MagicMock()
        self.backend_issue.raw = raw_data

        # Service Desk API format compatibility
        self.backend_issue.issueKey = raw_data.get("issueKey")
        self.backend_issue.issueId = raw_data.get("issueId")
        self.backend_issue.currentStatus = raw_data.get("currentStatus", {})

        # For backwards compatibility, also populate fields from requestFieldValues
        self.backend_issue.fields = mock.MagicMock()
        # Convert requestFieldValues array to fields object for compatibility
        fields_dict = {}
        for field_value in raw_data.get("requestFieldValues", []):
            field_id = field_value.get("fieldId")
            if field_id:
                fields_dict[field_id] = field_value.get("value")

        # Add any SLA fields
        sla_data = raw_data.get("sla", {})
        for sla_field, sla_value in sla_data.items():
            fields_dict[sla_field] = sla_value

        for field, value in fields_dict.items():
            setattr(self.backend_issue.fields, field, value)

        # Mock backend attachment using the raw JSON data
        raw_data = load_json_resource("jira_attachment_raw.json", __name__)
        self.backend_attachment = mock.MagicMock()
        self.backend_attachment.raw = raw_data
        for field, value in raw_data.items():
            setattr(self.backend_attachment, field, value)
        self.backend_issue.fields.attachment.append(self.backend_attachment)

        self.backend = ServiceDeskBackend()

        # Mock the manager methods
        self.backend.manager = mock.MagicMock()
        # Mock get_customer_request to return the issue
        self.backend.manager.get_customer_request.return_value = {
            "issueKey": "TST-16",
            "requestFieldValues": [],
            "currentStatus": {"status": "Open"},
            "_links": {"agent": "https://example.com/browse/TST-16"},
        }

        # Mock the get method used for attachments with dynamic response
        def get_attachments_mock(*args, **kwargs):
            # Return attachments based on current state of backend_issue.fields.attachment
            attachment_list = getattr(self.backend_issue.fields, "attachment", [])
            if not attachment_list:
                return {"values": []}

            return {
                "values": [
                    {
                        "_links": {
                            "jiraRest": f"https://example.com/rest/api/2/attachment/{self.backend_attachment.id}",
                            "content": "https://example.com/attachment/content",
                        },
                        "filename": self.backend_attachment.filename,
                        "created": {"iso8601": "2023-01-01T00:00:00Z"},
                        "author": {"accountId": "test-author"},
                    }
                ]
            }

        self.backend.manager.get.side_effect = get_attachments_mock

        file_content = BytesIO(
            base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        )
        path = mock.patch.object(
            AttachmentSynchronizer,
            "_download_file",
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
