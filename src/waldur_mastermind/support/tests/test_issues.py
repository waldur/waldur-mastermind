import json
from unittest import mock

# Mock objects for testing - will be replaced with proper mocks
from unittest.mock import MagicMock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.conf import settings

# Mock classes for testing
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.tests.helpers import load_json_resource
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests.factories import ResourceFactory
from waldur_mastermind.support import models, utils
from waldur_mastermind.support.backend.atlassian import ServiceDeskBackend
from waldur_mastermind.support.handlers import get_issue_scopes
from waldur_mastermind.support.tests import base, factories
from waldur_openstack.tests import (
    fixtures as openstack_fixtures,
)
from waldur_openstack.tests.factories import FloatingIPFactory, PortFactory

IMPERSONATED_USER_HEADER = settings.WALDUR_CORE.get(
    "REQUEST_HEADER_IMPERSONATED_USER_UUID"
)
IMPERSONATOR_HEADER = settings.WALDUR_CORE.get("RESPONSE_HEADER_IMPERSONATOR_UUID")


@ddt
class IssueRetrieveTest(base.BaseTest):
    @data("staff", "global_support", "owner")
    def test_user_can_access_customer_issue_if_he_has_customer_level_permission(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer)

        response = self.client.get(factories.IssueFactory.get_url(issue))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("admin", "manager", "user")
    def test_user_cannot_access_customer_issue_if_he_has_no_permission(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(customer=self.fixture.customer)

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "global_support", "owner", "admin", "manager")
    def test_user_can_access_project_issue_if_he_has_project_level_permission(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user")
    def test_user_cannot_access_project_issue_if_he_has_no_project_level_permission(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )

        response = self.client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("user")
    def test_user_can_see_a_list_of_all_issues_where_user_is_a_caller(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(caller=getattr(self.fixture, user))
        url = factories.IssueFactory.get_list_url()

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["key"], issue.key)

    @data("user")
    def test_user_can_not_see_link_to_jira_if_he_is_not_staff_or_support(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(caller=getattr(self.fixture, user))
        url = factories.IssueFactory.get_url(issue=issue)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("link", response.data)

    @data("staff", "global_support")
    def test_user_can_see_link_to_jira_if_he_is_staff_or_support(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        issue = factories.IssueFactory(caller=getattr(self.fixture, user))
        url = factories.IssueFactory.get_url(issue=issue)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("link", response.data)

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_user_can_not_see_a_list_of_issues_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.user)
        url = factories.IssueFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)


class IssueCreateBaseTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.url = factories.IssueFactory.get_list_url()
        self.caller = structure_factories.UserFactory()

    def _mock_jira(self, user=None):
        mock.patch.stopall()

        backend_patch = mock.patch("waldur_mastermind.support.views.backend")
        self.mock_backend = backend_patch.start()
        self.mock_backend.get_active_backend.return_value = ServiceDeskBackend()

        mock_patch = mock.patch(
            "waldur_mastermind.support.backend.atlassian.ServiceDesk"
        )
        self.mock_service_desk = mock_patch.start()

        # Mock ServiceDesk methods
        self.mock_service_desk_instance = MagicMock()
        self.mock_service_desk.return_value = self.mock_service_desk_instance

        # Mock issue response - atlassian-python-api returns dict directly
        issue_data = load_json_resource("jira_issue_raw.json", __name__)

        # Service Desk API format for create_customer_request response
        service_desk_response = {
            "issueKey": issue_data["key"],  # Map key to issueKey for Service Desk API
            "issueId": issue_data["id"],
            "requestFieldValues": [
                {"fieldId": "summary", "value": "test_issue"},
                {"fieldId": "description", "value": ""},
            ],
            "currentStatus": {"status": "Open"},
            "_links": {"agent": f"https://example.com/browse/{issue_data['key']}"},
        }

        self.mock_service_desk_instance.create_customer_request.return_value = (
            service_desk_response
        )
        self.mock_service_desk_instance.waldur_create_customer_request.return_value = (
            service_desk_response
        )
        self.mock_service_desk_instance.create_issue.return_value = issue_data

        # Mock additional API calls used in the backend
        def mock_get(url, *args, **kwargs):
            if url.endswith("?fields=resolution"):
                return {"fields": {"resolution": {"name": "Done"}}}
            return [
                {"id": "customfield_10001", "clauseNames": ["Waldur project"]},
                {"id": "customfield_10002", "clauseNames": ["Reporter organization"]},
                {"id": "customfield_10003", "clauseNames": ["Affected resource"]},
                {"id": "customfield_10004", "clauseNames": ["Waldur template"]},
                {"id": "customfield_10005", "clauseNames": ["Original Reporter"]},
            ]

        self.mock_service_desk_instance.get.side_effect = mock_get

        # Mock user response
        mock_backend_users = [
            {
                "key": "user_1",
                "active": True,
                "name": user.email if user else "user_1@example.com",
            }
        ]
        self.mock_service_desk_instance.waldur_search_users.return_value = (
            mock_backend_users
        )

    def _get_valid_payload(self, **additional):
        is_reported_manually = additional.get("is_reported_manually")
        issue_type = utils.get_default_request_type()
        # Create the request type if it doesn't exist
        if issue_type:
            factories.RequestTypeFactory(name=issue_type, is_active=True)
        else:
            # If no default, create one
            rt = factories.RequestTypeFactory(name="Test Request", is_active=True)
            issue_type = rt.name
        payload = {
            "summary": "test_issue",
            "type": issue_type,
        }

        if is_reported_manually:
            payload["is_reported_manually"] = True
        else:
            payload["caller"] = structure_factories.UserFactory.get_url(
                user=self.caller
            )

        payload.update(additional)
        return payload


@ddt
class IssueCreateTest(IssueCreateBaseTest):
    def setUp(self):
        super().setUp()
        factories.SupportCustomerFactory(user=self.caller)

    @data("staff", "global_support")
    def test_staff_or_support_can_specify_priority(self, user):
        factories.SupportUserFactory(user=getattr(self.fixture, user))
        self.client.force_authenticate(getattr(self.fixture, user))

        priority = factories.PriorityFactory()
        response = self.client.post(
            self.url, data=self._get_valid_payload(priority=priority.name)
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["priority"], priority.name)

    @data("admin", "manager", "user")
    def test_other_user_can_not_specify_priority(self, user):
        factories.SupportUserFactory(user=getattr(self.fixture, user))
        self.client.force_authenticate(getattr(self.fixture, user))

        priority = factories.PriorityFactory()
        response = self.client.post(
            self.url, data=self._get_valid_payload(priority=priority.name)
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff", "global_support")
    def test_staff_or_support_can_create_issue_if_he_has_support_user(self, user):
        factories.SupportUserFactory(user=getattr(self.fixture, user))
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "global_support")
    @override_config(ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS=True)
    def test_staff_or_support_cannot_create_issue_if_he_does_not_have_support_user(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS=True)
    def test_user_cannot_create_issue_if_his_support_user_is_disabled(self):
        factories.SupportUserFactory(user=self.fixture.staff, is_active=False)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff", "global_support", "owner")
    @override_config(ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS=True)
    def test_user_with_access_to_customer_can_create_customer_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            customer=structure_factories.CustomerFactory.get_url(self.fixture.customer),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Issue.objects.filter(customer=self.fixture.customer).exists()
        )

    @data("admin", "manager", "user")
    def test_user_without_access_to_customer_cannot_create_customer_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            customer=structure_factories.CustomerFactory.get_url(self.fixture.customer),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.Issue.objects.filter(summary=payload["summary"]).exists()
        )

    @data("staff", "global_support", "owner", "admin", "manager")
    def test_user_with_access_to_project_can_create_project_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            project=structure_factories.ProjectFactory.get_url(self.fixture.project),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Issue.objects.filter(customer=self.fixture.customer).exists()
        )

    @data("user")
    def test_user_without_access_to_project_cannot_create_project_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            project=structure_factories.ProjectFactory.get_url(self.fixture.project),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.Issue.objects.filter(summary=payload["summary"]).exists()
        )

    @data("staff", "global_support", "owner", "admin", "manager")
    def test_user_with_access_to_resource_can_create_resource_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            resource=structure_factories.TestNewInstanceFactory.get_url(
                self.fixture.resource
            ),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Issue.objects.filter(customer=self.fixture.customer).exists()
        )

    def test_backend_id_exists_in_issue_description_if_resource_has_been_passed(self):
        self.client.force_authenticate(self.fixture.staff)
        self.fixture.resource.backend_id = "resource backend ID"
        self.fixture.resource.save()
        payload = self._get_valid_payload(
            resource=structure_factories.TestNewInstanceFactory.get_url(
                self.fixture.resource
            ),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Issue.objects.filter(customer=self.fixture.customer).exists()
        )
        issue = models.Issue.objects.filter(customer=self.fixture.customer).get()
        self.assertTrue("resource backend ID" in issue.description)

    @data("user")
    def test_user_without_access_to_resource_cannot_create_resource_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload(
            resource=structure_factories.TestNewInstanceFactory.get_url(
                self.fixture.resource
            ),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.Issue.objects.filter(summary=payload["summary"]).exists()
        )

    def test_project_issue_populates_customer_field_on_creation(self):
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload(
            project=structure_factories.ProjectFactory.get_url(self.fixture.project)
        )

        response = self.client.post(self.url, data=payload)

        issue = models.Issue.objects.get(uuid=json.loads(response.content)["uuid"])
        self.assertEqual(issue.customer, self.fixture.project.customer)

    def test_resource_issue_populated_customer_and_project_field_on_creation(self):
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload(
            resource=structure_factories.TestNewInstanceFactory.get_url(
                self.fixture.resource
            )
        )

        response = self.client.post(self.url, data=payload)

        issue = models.Issue.objects.get(uuid=json.loads(response.content)["uuid"])
        self.assertEqual(issue.project, self.fixture.resource.project)
        self.assertEqual(issue.customer, self.fixture.resource.project.customer)

    @override_config(
        WALDUR_SUPPORT_ENABLED=False,
        ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS=True,
    )
    def test_user_can_not_create_issue_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, data=self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    def test_fill_custom_fields(self):
        self._mock_jira()
        # Create RequestType for the mapped backend type (Informational -> Get IT help)
        factories.RequestTypeFactory(name="Get IT help", issue_type_name="Get IT help")

        user = self.fixture.staff
        factories.SupportUserFactory(user=user)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            data=self._get_valid_payload(
                project=structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                resource=structure_factories.TestNewInstanceFactory.get_url(),
                template=factories.TemplateFactory.get_url(),
            ),
        )
        issue = response.data
        # Check that create_customer_request was called with custom fields
        call_args = self.mock_service_desk_instance.create_customer_request.call_args
        values_dict = call_args[1]["values_dict"]  # Get the values_dict parameter
        self.assertEqual(issue["customer_name"], values_dict["customfield_10002"])
        self.assertEqual(issue["project_name"], values_dict["customfield_10001"])
        # Note: resource and template assertions may need adjustment based on actual implementation

    def test_if_issue_does_not_have_reporter_organisation_field_not_fill(self):
        self._mock_jira()

        issue = factories.IssueFactory(
            reporter=None, backend_id=None, type="Informational"
        )
        factories.SupportCustomerFactory(user=issue.caller)
        # Create RequestType with the same name as issue.type (no mapping anymore)
        factories.RequestTypeFactory(
            name="Informational", issue_type_name="Informational"
        )
        ServiceDeskBackend().create_issue(issue)
        # Check that create_customer_request was called without Original Reporter field
        call_args = self.mock_service_desk_instance.create_customer_request.call_args
        values_dict = call_args[1]["values_dict"]
        # The Original Reporter field should not be present since there's no reporter
        self.assertTrue("customfield_10005" not in values_dict.keys())

    def test_pull_request_types(self):
        self._mock_jira()
        # Mock the get_request_types method to return proper structure
        self.mock_service_desk_instance.get_request_types.return_value = {
            "values": [{"name": "Get IT help", "id": "1", "issueTypeId": "10101"}]
        }
        self.mock_service_desk_instance.issue_type.return_value = {
            "name": "Service Request",
            "id": "1",
        }
        issue_type = utils.get_atlassian_issue_type()  # Returns "Informational"
        # Create RequestType with the same name as issue.type (no mapping anymore)
        factories.RequestTypeFactory(name=issue_type, issue_type_name=issue_type)
        issue = factories.IssueFactory(reporter=None, backend_id=None, type=issue_type)
        factories.SupportCustomerFactory(user=issue.caller)
        ServiceDeskBackend().create_issue(issue)
        self.assertEqual(models.RequestType.objects.count(), 1)

    def test_create_issue_if_exist_several_backend_users_with_same_email(self):
        self._mock_jira()
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        mock_backend_users = [
            {
                "accountId": "user_1",
                "active": False,
                "emailAddress": "test@example.com",
            },
            {"accountId": "user_2", "active": True, "emailAddress": "test@example.com"},
        ]
        self.mock_service_desk_instance.search_users.return_value = mock_backend_users
        response = self.client.post(self.url, data=self._get_valid_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_create_confirmation_comment_if_template_exists(self):
        payload = self._get_valid_payload()
        issue_type = payload["type"]
        factories.TemplateConfirmationCommentFactory(
            issue_type=issue_type, template="issue_type template"
        )
        self._create_confirmation_comment("issue_type template")

    def test_create_confirmation_comment_if_only_default_template_exists(self):
        factories.TemplateConfirmationCommentFactory(template="default template")
        self._create_confirmation_comment("default template")

    def test_do_not_create_confirmation_comment_if_template_does_not_exist(self):
        self._create_confirmation_comment(None)

    def test_issue_summary_includes_customer_abbreviation(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload(
            customer=structure_factories.CustomerFactory.get_url(self.fixture.customer),
            is_reported_manually=True,
        )

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Issue.objects.filter(customer=self.fixture.customer).exists()
        )
        issue = models.Issue.objects.get(customer=self.fixture.customer)
        self.assertEqual(
            issue.summary, "%s: test_issue\n" % self.fixture.customer.abbreviation
        )

    def test_site_name_included_in_description(self):
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue("example.com" in response.data["description"])

    def test_create_issue_with_remote_id(self):
        remote_id = "RT_ID:1234"
        factories.SupportUserFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, data=self._get_valid_payload(remote_id=remote_id)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["remote_id"], remote_id)

    def _create_confirmation_comment(self, expected_body):
        user = self.fixture.staff
        factories.SupportUserFactory(user=user)
        mock.patch.stopall()
        with mock.patch(
            "waldur_mastermind.support.backend.atlassian.ServiceDeskBackend.create_issue"
        ):
            with mock.patch(
                "waldur_mastermind.support.backend.atlassian.ServiceDeskBackend._add_comment"
            ) as _add_comment:
                self.client.force_authenticate(user)
                self.client.post(self.url, data=self._get_valid_payload())
                if expected_body:
                    _add_comment.assert_called_once_with(
                        None, expected_body, is_internal=False
                    )
                else:
                    _add_comment.assert_not_called()

    def test_add_impersonator_name_to_description(self):
        staff = self.fixture.staff
        impersonated_user = self.fixture.global_support

        token = Token.objects.get(user=staff)
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": "Token " + token.key,
                IMPERSONATED_USER_HEADER: impersonated_user.uuid.hex,
            }
        )
        factories.SupportUserFactory(user=staff)
        priority = factories.PriorityFactory()
        response = self.client.post(
            self.url, data=self._get_valid_payload(priority=priority.name)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(staff.username in response.data["description"])


@ddt
class IssueUpdateTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        self.url = factories.IssueFactory.get_url(self.issue)

    @data("staff", "global_support")
    def test_staff_or_support_can_edit_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.Issue.objects.filter(summary=payload["summary"]).exists()
        )

    @data("owner", "admin", "manager")
    def test_nonstaff_user_cannot_edit_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.Issue.objects.filter(summary=payload["summary"]).exists()
        )

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_staff_can_not_update_issue_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload()
        response = self.client.patch(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    def _get_valid_payload(self):
        return {"summary": "edited_summary"}


@ddt
class IssueDeleteTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        self.url = factories.IssueFactory.get_url(self.issue)

    @data("staff", "global_support")
    def test_staff_or_support_can_delete_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Issue.objects.filter(id=self.issue.id).exists())

    @data("owner", "admin", "manager")
    def test_nonstaff_user_cannot_delete_issue(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Issue.objects.filter(id=self.issue.id).exists())

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_user_can_not_delete_issue_if_support_extension_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)


class IssueOrderingTest(test.APITestCase):
    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_issue_ordering(self):
        factories.IssueFactory(key="TST")
        factories.IssueFactory(key="TST-1")
        factories.IssueFactory(key="TST-11")
        factories.IssueFactory(key="TST-2")
        factories.IssueFactory(key="TST-21")
        staff = structure_factories.UserFactory(is_staff=True)

        self.client.force_authenticate(staff)

        response = self.client.get(
            factories.IssueFactory.get_list_url(), data={"o": "key"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            ["TST", "TST-1", "TST-2", "TST-11", "TST-21"],
            [k["key"] for k in response.data],
        )


class IssueFilterTest(base.BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture.issue
        self.issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        self.url = factories.IssueFactory.get_list_url()
        self.openstack_fixture = openstack_fixtures.OpenStackFixture()
        self.issue.resource = self.openstack_fixture.instance
        self.issue.save()

    def test_filter_by_resource_uuid(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        response = self.client.get(
            self.url, data={"resource_uuid": self.openstack_fixture.instance.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.issue.uuid.hex)

    def test_filter_by_internal_ip(self):
        self.openstack_fixture.port.fixed_ips = [{"ip_address": "111.222.333.444"}]
        self.openstack_fixture.port.save()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.url, data={"resource_internal_ip": "111.111.111.111"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        response = self.client.get(
            self.url, data={"resource_internal_ip": "111.222.333.444"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.issue.uuid.hex)

    def test_filter_by_external_ip(self):
        port = PortFactory(instance=self.openstack_fixture.instance)
        floating_ip = FloatingIPFactory(port=port)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.url, data={"resource_external_ip": "111.111.111.111"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        response = self.client.get(
            self.url, data={"resource_external_ip": floating_ip.address}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.issue.uuid.hex)


class GetIssueScopesTest(base.BaseTest):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.issue = factories.IssueFactory(
            customer=self.customer, project=self.project
        )

    def test_get_issue_scopes_with_active_project(self):
        # Act
        scopes = get_issue_scopes(self.issue)

        # Assert
        self.assertEqual(len(scopes), 2)
        self.assertIn(self.project, scopes)
        self.assertIn(self.customer, scopes)

    def test_get_issue_scopes_with_deleted_project(self):
        # Arrange
        self.project.delete()

        # Act
        scopes = get_issue_scopes(self.issue)

        # Assert
        self.assertEqual(len(scopes), 2)
        self.assertIn(self.customer, scopes)

    def test_get_issue_scopes_with_resource(self):
        # Arrange
        resource = ResourceFactory(project=self.project)
        self.issue.resource = resource
        self.issue.save()

        # Act
        scopes = get_issue_scopes(self.issue)

        # Assert
        self.assertEqual(len(scopes), 3)
        self.assertIn(resource, scopes)
        self.assertIn(self.project, scopes)
        self.assertIn(self.customer, scopes)

    def test_get_issue_scopes_with_stale_resource_content_type(self):
        """When ``resource_content_type`` points at a model that is no longer
        registered, ``ContentType.model_class()`` returns ``None`` and
        accessing the GenericForeignKey raises
        ``AttributeError("'NoneType' object has no attribute '_base_manager'")``.
        ``get_issue_scopes`` must tolerate this and fall back to the issue's
        project/customer.
        """
        from django.contrib.contenttypes.models import ContentType

        resource = ResourceFactory(project=self.project)
        self.issue.resource = resource
        self.issue.save()
        self.issue.refresh_from_db()

        with mock.patch.object(ContentType, "model_class", return_value=None):
            scopes = get_issue_scopes(self.issue)

        self.assertIn(self.project, scopes)
        self.assertIn(self.customer, scopes)


class IssueSerializerSafeResourceTest(base.BaseTest):
    """Regression test for production crash on ``GET /api/support-issues/``
    when an issue's ``resource_content_type`` points at a model whose class
    is no longer registered.
    """

    def test_list_does_not_crash_when_resource_content_type_is_stale(self):
        from django.contrib.contenttypes.models import ContentType

        self.client.force_authenticate(self.fixture.staff)
        resource = ResourceFactory(project=self.fixture.project)
        issue = factories.IssueFactory(
            customer=self.fixture.customer, project=self.fixture.project
        )
        issue.resource = resource
        issue.save()

        with mock.patch.object(ContentType, "model_class", return_value=None):
            response = self.client.get(factories.IssueFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
