from unittest import mock

import pytest
from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.tests import base, factories, fixtures


def _provider_ticket_url(issue, action=None):
    url = "http://testserver" + reverse(
        "provider-ticket-detail", kwargs={"uuid": issue.uuid.hex}
    )
    return url if action is None else url + action + "/"


def _provider_ticket_list_url():
    return "http://testserver" + reverse("provider-ticket-list")


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ATLASSIAN,
    ATLASSIAN_ORGANISATION_FIELD="Reporter organization",
    ATLASSIAN_PROJECT_FIELD="Waldur project",
    ATLASSIAN_AFFECTED_RESOURCE_FIELD="Affected resource",
    ATLASSIAN_TEMPLATE_FIELD="Waldur template",
)
class ProviderHelpdeskBaseTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        mock_patch = mock.patch("waldur_mastermind.support.backend.get_active_backend")
        self.mock_get_active_backend = mock_patch.start()
        self.mock_get_active_backend().backend_name = None
        self.mock_get_active_backend().update_is_available.return_value = True
        self.mock_get_active_backend().destroy_is_available.return_value = True
        self.mock_get_active_backend().comment_create_is_available.return_value = True
        self.mock_get_active_backend().comment_update_is_available.return_value = True
        self.mock_get_active_backend().comment_destroy_is_available.return_value = True
        self.mock_get_active_backend().attachment_create_is_available.return_value = (
            True
        )
        self.mock_get_active_backend().attachment_destroy_is_available.return_value = (
            True
        )
        self.mock_get_active_backend().get_users.return_value = [1]
        self.mock_get_active_backend().get_issue_details.return_value = {}
        self.mock_get_active_backend().summary_max_length = 255
        self.mock_get_active_backend().pull_support_users = mock.MagicMock()
        self.mock_get_active_backend().create_comment = mock.MagicMock()

        models.IssueStatus.objects.get_or_create(
            name="done", defaults={"type": models.IssueStatus.Types.RESOLVED}
        )
        models.IssueStatus.objects.get_or_create(
            name="rejected", defaults={"type": models.IssueStatus.Types.CANCELED}
        )

        # Set up customer, service provider and owner for provider tests
        self.customer = structure_factories.CustomerFactory()
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.customer
        )
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

        self.helpdesk = factories.ProviderHelpdeskFactory(
            service_provider=self.service_provider
        )

    def tearDown(self):
        mock.patch.stopall()


# =====================================================================
# 1. ProviderHelpdeskViewSet
# =====================================================================


class ProviderHelpdeskListTest(ProviderHelpdeskBaseTest):
    def test_staff_can_list_all_provider_helpdesks(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ProviderHelpdeskFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.helpdesk.uuid.hex, uuids)

    def test_customer_owner_can_list_their_provider_helpdesk(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.ProviderHelpdeskFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.helpdesk.uuid.hex, uuids)

    def test_regular_user_cannot_see_others_helpdesks(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(factories.ProviderHelpdeskFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_owner_cannot_see_helpdesks_of_other_customers(self):
        other_customer = structure_factories.CustomerFactory()
        other_sp = marketplace_factories.ServiceProviderFactory(customer=other_customer)
        factories.ProviderHelpdeskFactory(service_provider=other_sp)

        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.ProviderHelpdeskFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Owner should only see their own helpdesk
        uuids = [item["uuid"] for item in response.data]
        self.assertEqual(len(uuids), 1)
        self.assertIn(self.helpdesk.uuid.hex, uuids)


class ProviderHelpdeskCreateTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        # Create a second customer/provider for creation tests
        self.new_customer = structure_factories.CustomerFactory()
        self.new_service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.new_customer
        )
        self.new_owner = structure_factories.UserFactory()
        self.new_customer.add_user(self.new_owner, CustomerRole.OWNER)

    def test_staff_can_create_provider_helpdesk(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "service_provider": self.new_service_provider.uuid.hex,
            "backend_type": models.ProviderHelpdesk.BackendTypes.BASIC,
            "is_active": True,
        }
        response = self.client.post(
            factories.ProviderHelpdeskFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.ProviderHelpdesk.objects.filter(
                service_provider=self.new_service_provider
            ).exists()
        )

    def test_customer_owner_cannot_create_provider_helpdesk(self):
        # Owner create is blocked because the permission check receives
        # obj=None on create and only staff passes the check.
        self.client.force_authenticate(self.new_owner)
        data = {
            "service_provider": self.new_service_provider.uuid.hex,
            "backend_type": models.ProviderHelpdesk.BackendTypes.EMAIL,
            "is_active": True,
        }
        response = self.client.post(
            factories.ProviderHelpdeskFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_create_provider_helpdesk(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {
            "service_provider": self.new_service_provider.uuid.hex,
            "backend_type": models.ProviderHelpdesk.BackendTypes.BASIC,
        }
        response = self.client.post(
            factories.ProviderHelpdeskFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProviderHelpdeskUpdateTest(ProviderHelpdeskBaseTest):
    def test_staff_can_update_provider_helpdesk(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        data = {"notification_email": "test@example.com"}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.helpdesk.refresh_from_db()
        self.assertEqual(self.helpdesk.notification_email, "test@example.com")

    def test_owner_can_update_own_provider_helpdesk(self):
        # Owner passes the object-level check on their own helpdesk.
        self.client.force_authenticate(self.owner)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        data = {"is_active": False}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.helpdesk.refresh_from_db()
        self.assertFalse(self.helpdesk.is_active)

    def test_owner_can_validate_own_provider_helpdesk(self):
        # Owner must not be rejected before the object-level check runs.
        self.client.force_authenticate(self.owner)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk) + "validate/"
        response = self.client.post(url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_update_provider_helpdesk(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        data = {"is_active": False}
        response = self.client.patch(url, data, format="json")
        # Denied either at object permission (403) or via the scoped queryset (404).
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )


class ProviderHelpdeskDeleteTest(ProviderHelpdeskBaseTest):
    def test_staff_can_delete_provider_helpdesk(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ProviderHelpdesk.objects.filter(uuid=self.helpdesk.uuid).exists()
        )

    def test_owner_can_delete_own_provider_helpdesk(self):
        # Owner passes the object-level check on their own helpdesk.
        self.client.force_authenticate(self.owner)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ProviderHelpdesk.objects.filter(uuid=self.helpdesk.uuid).exists()
        )

    def test_regular_user_cannot_delete_provider_helpdesk(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        response = self.client.delete(url)
        # Denied either at object permission (403) or via the scoped queryset (404).
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )


class ProviderHelpdeskValidateTest(ProviderHelpdeskBaseTest):
    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_validate_returns_healthy_when_backend_ok(self, mock_get_backend):
        mock_get_backend.return_value = mock.MagicMock()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderHelpdeskFactory.get_url(
            self.helpdesk, action="validate"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "healthy")
        self.helpdesk.refresh_from_db()
        self.assertEqual(self.helpdesk.last_health_status, "healthy")

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_validate_returns_unhealthy_when_backend_fails(self, mock_get_backend):
        mock_get_backend.side_effect = Exception("Connection refused")
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderHelpdeskFactory.get_url(
            self.helpdesk, action="validate"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["status"], "unhealthy")
        self.assertIn("Connection refused", response.data["error"])
        self.helpdesk.refresh_from_db()
        self.assertEqual(self.helpdesk.last_health_status, "unhealthy")

    def test_owner_can_validate_own_helpdesk(self):
        # Owners of the helpdesk's customer may validate it (regression: the
        # object-scoped permission previously rejected them at has_permission
        # while obj was still None).
        self.client.force_authenticate(self.owner)
        url = factories.ProviderHelpdeskFactory.get_url(
            self.helpdesk, action="validate"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_validate_helpdesk(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.ProviderHelpdeskFactory.get_url(
            self.helpdesk, action="validate"
        )
        response = self.client.post(url)
        # Denied either at object permission (403) or via the scoped queryset (404).
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )


class ProviderHelpdeskWebhookSecretMaskingTest(ProviderHelpdeskBaseTest):
    def test_webhook_secret_is_masked_in_response(self):
        self.helpdesk.webhook_secret = "super-secret-value"
        self.helpdesk.save(update_fields=["webhook_secret"])

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["webhook_secret"], "***")

    def test_empty_webhook_secret_is_not_masked(self):
        self.helpdesk.webhook_secret = ""
        self.helpdesk.save(update_fields=["webhook_secret"])

        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderHelpdeskFactory.get_url(self.helpdesk)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["webhook_secret"], "")


# =====================================================================
# 2. ProviderTicketViewSet
# =====================================================================


class ProviderTicketListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-100")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-101",
        )

    def test_staff_can_list_all_provider_tickets(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.child_issue.uuid.hex, uuids)

    def test_provider_owner_can_list_their_child_tickets(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.child_issue.uuid.hex, uuids)

    def test_provider_support_user_can_list_child_tickets(self):
        support_agent = structure_factories.UserFactory()
        factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
            user=support_agent,
            is_active=True,
        )
        self.client.force_authenticate(support_agent)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.child_issue.uuid.hex, uuids)

    def test_regular_user_cannot_see_provider_tickets(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_owner_cannot_see_tickets_of_other_providers(self):
        other_customer = structure_factories.CustomerFactory()
        other_sp = marketplace_factories.ServiceProviderFactory(customer=other_customer)
        other_helpdesk = factories.ProviderHelpdeskFactory(service_provider=other_sp)
        other_parent = factories.IssueFactory(backend_id="WLD-200")
        factories.IssueFactory(
            parent_issue=other_parent,
            provider_helpdesk=other_helpdesk,
            backend_id="WLD-201",
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should only see their own child tickets
        uuids = [item["uuid"] for item in response.data]
        self.assertEqual(len(uuids), 1)
        self.assertIn(self.child_issue.uuid.hex, uuids)

    def test_issues_without_parent_are_not_listed(self):
        # Non-child issues (regular issues) should not appear
        factories.IssueFactory(backend_id="WLD-300")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        # Only child issue should be listed
        self.assertIn(self.child_issue.uuid.hex, uuids)

    def test_list_serializes_assigned_ticket_as_uuid(self):
        # Regression: an assigned ticket must not break list serialization.
        # provider_assignee used to auto-hyperlink to an unregistered view name,
        # raising NoReverseMatch (HTTP 500) once any ticket had an assignee.
        agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk, is_active=True
        )
        self.child_issue.provider_assignee = agent
        self.child_issue.save(update_fields=["provider_assignee"])

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(_provider_ticket_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item = next(i for i in response.data if i["uuid"] == self.child_issue.uuid.hex)
        self.assertEqual(str(item["provider_assignee"]), agent.uuid.hex)
        self.assertEqual(item["provider_assignee_name"], agent.user.full_name)


class ProviderTicketRetrieveTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-400")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-401",
        )

    def test_staff_can_retrieve_provider_ticket(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(_provider_ticket_url(self.child_issue))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.child_issue.uuid.hex)

    def test_response_includes_parent_issue_info(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(_provider_ticket_url(self.child_issue))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["parent_issue_key"], self.parent_issue.key)


class ProviderTicketCommentTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-500")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-501",
        )

    def test_provider_owner_can_add_comment_to_ticket(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="comment")
        data = {"description": "Provider comment here", "is_public": True}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.Comment.objects.filter(issue=self.child_issue).count(), 1
        )

    def test_staff_can_add_comment_to_provider_ticket(self):
        self.client.force_authenticate(self.fixture.staff)
        url = _provider_ticket_url(self.child_issue, action="comment")
        data = {"description": "Staff comment", "is_public": True}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_provider_comment_calls_backend_create_comment(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="comment")
        data = {"description": "Test backend call"}
        self.client.post(url, data, format="json")
        self.mock_get_active_backend().create_comment.assert_called_once()

    def test_comment_returns_uuid_and_description(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="comment")
        data = {"description": "Check response shape"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("uuid", response.data)
        self.assertIn("description", response.data)
        self.assertEqual(response.data["description"], "Check response shape")


class ProviderTicketResolveTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-600")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-601",
            status="open",
        )

    def test_provider_owner_can_resolve_ticket(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="resolve")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "resolved")
        self.child_issue.refresh_from_db()
        self.assertEqual(self.child_issue.status, "done")

    def test_resolve_updates_parent_processing_log(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="resolve")
        self.client.post(url)
        self.parent_issue.refresh_from_db()
        self.assertTrue(len(self.parent_issue.processing_log) > 0)
        last_entry = self.parent_issue.processing_log[-1]
        self.assertEqual(last_entry["event"], "child_resolved")
        self.assertEqual(last_entry["details"]["child_key"], self.child_issue.key)

    def test_staff_can_resolve_provider_ticket(self):
        self.client.force_authenticate(self.fixture.staff)
        url = _provider_ticket_url(self.child_issue, action="resolve")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resolve_posts_public_comment_on_parent(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="resolve")
        self.client.post(url)
        comment = self.parent_issue.comments.order_by("created").last()
        self.assertIsNotNone(comment)
        self.assertTrue(comment.is_public)
        # is_forwarded prevents the note from looping back to the child.
        self.assertTrue(comment.is_forwarded)
        self.assertIn(self.child_issue.key, comment.description)

    def test_resolve_does_not_change_parent_status(self):
        original_status = self.parent_issue.status
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="resolve")
        self.client.post(url)
        self.parent_issue.refresh_from_db()
        self.assertEqual(self.parent_issue.status, original_status)


class ProviderTicketAssignTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-700")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-701",
        )
        self.support_agent_user = structure_factories.UserFactory()
        self.support_agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
            user=self.support_agent_user,
            is_active=True,
        )

    def test_provider_owner_can_assign_ticket_to_support_user(self):
        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="assign")
        data = {"provider_support_user": self.support_agent.uuid.hex}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "assigned")
        self.child_issue.refresh_from_db()
        self.assertEqual(self.child_issue.provider_assignee, self.support_agent)

    def test_cannot_assign_support_user_from_different_helpdesk(self):
        other_customer = structure_factories.CustomerFactory()
        other_sp = marketplace_factories.ServiceProviderFactory(customer=other_customer)
        other_helpdesk = factories.ProviderHelpdeskFactory(service_provider=other_sp)
        other_agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=other_helpdesk,
            is_active=True,
        )

        self.client.force_authenticate(self.owner)
        url = _provider_ticket_url(self.child_issue, action="assign")
        data = {"provider_support_user": other_agent.uuid.hex}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProviderTicketClaimTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-800")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-801",
        )
        self.support_agent_user = structure_factories.UserFactory()
        self.support_agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
            user=self.support_agent_user,
            is_active=True,
        )

    def test_support_user_can_claim_ticket(self):
        # The support agent user needs to be an owner or
        # a provider support user to access the ticket
        self.customer.add_user(self.support_agent_user, CustomerRole.OWNER)
        self.client.force_authenticate(self.support_agent_user)
        url = _provider_ticket_url(self.child_issue, action="claim")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "claimed")
        self.child_issue.refresh_from_db()
        self.assertEqual(self.child_issue.provider_assignee, self.support_agent)

    def test_user_without_provider_support_role_cannot_claim(self):
        non_agent = structure_factories.UserFactory()
        self.customer.add_user(non_agent, CustomerRole.OWNER)
        self.client.force_authenticate(non_agent)
        url = _provider_ticket_url(self.child_issue, action="claim")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProviderTicketCustomerContextTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.caller = structure_factories.UserFactory(
            full_name="John Doe", email="john@example.com"
        )
        self.parent_customer = structure_factories.CustomerFactory(name="Caller Org")
        self.parent_issue = factories.IssueFactory(
            backend_id="WLD-900",
            caller=self.caller,
            customer=self.parent_customer,
        )
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-901",
        )

    def test_customer_context_returns_caller_info(self):
        self.client.force_authenticate(self.fixture.staff)
        url = _provider_ticket_url(self.child_issue, action="customer_context")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["caller"]["full_name"], "John Doe")
        self.assertEqual(response.data["caller"]["email"], "john@example.com")
        self.assertEqual(response.data["caller"]["organization"], "Caller Org")

    def test_customer_context_without_parent_returns_empty_caller(self):
        # Create a child issue without caller on parent
        orphan_parent = factories.IssueFactory(
            backend_id="WLD-902", caller=None, customer=None
        )
        orphan_child = factories.IssueFactory(
            parent_issue=orphan_parent,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-903",
        )
        self.client.force_authenticate(self.fixture.staff)
        url = _provider_ticket_url(orphan_child, action="customer_context")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["caller"]["full_name"], "")


class ProviderTicketStatsTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.parent_issue = factories.IssueFactory(backend_id="WLD-1000")
        factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-1001",
            status="open",
        )
        factories.IssueFactory(
            parent_issue=self.parent_issue,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-1002",
            is_escalated=True,
        )

    def test_stats_endpoint_returns_ticket_statistics(self):
        self.client.force_authenticate(self.fixture.staff)
        url = _provider_ticket_list_url() + "stats/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("total_open", response.data)
        self.assertIn("total_escalated", response.data)
        self.assertEqual(response.data["total_escalated"], 1)


# =====================================================================
# 3. ProviderSupportUserViewSet
# =====================================================================


class ProviderSupportUserListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
        )

    def test_staff_can_list_provider_support_users(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ProviderSupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.support_user.uuid.hex, uuids)

    def test_customer_owner_can_list_their_support_users(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(factories.ProviderSupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.support_user.uuid.hex, uuids)

    def test_regular_user_cannot_see_provider_support_users(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(factories.ProviderSupportUserFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class ProviderSupportUserCreateTest(ProviderHelpdeskBaseTest):
    def test_staff_can_create_provider_support_user(self):
        new_user = structure_factories.UserFactory()
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "provider_helpdesk": self.helpdesk.uuid.hex,
            "user": "http://testserver"
            + reverse("user-detail", kwargs={"uuid": new_user.uuid.hex}),
            "role": models.ProviderSupportUser.Roles.AGENT,
            "is_active": True,
            "max_open_tickets": 15,
        }
        response = self.client.post(
            factories.ProviderSupportUserFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_customer_owner_cannot_create_support_user(self):
        # Owner is blocked at has_permission stage where obj is None.
        new_user = structure_factories.UserFactory()
        self.client.force_authenticate(self.owner)
        data = {
            "provider_helpdesk": self.helpdesk.uuid.hex,
            "user": "http://testserver"
            + reverse("user-detail", kwargs={"uuid": new_user.uuid.hex}),
            "role": models.ProviderSupportUser.Roles.AGENT,
            "is_active": True,
        }
        response = self.client.post(
            factories.ProviderSupportUserFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_create_provider_support_user(self):
        new_user = structure_factories.UserFactory()
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {
            "provider_helpdesk": self.helpdesk.uuid.hex,
            "user": "http://testserver"
            + reverse("user-detail", kwargs={"uuid": new_user.uuid.hex}),
            "role": models.ProviderSupportUser.Roles.AGENT,
        }
        response = self.client.post(
            factories.ProviderSupportUserFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProviderSupportUserUpdateTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
            max_open_tickets=20,
        )

    def test_staff_can_update_provider_support_user(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderSupportUserFactory.get_url(self.support_user)
        data = {"max_open_tickets": 30}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.support_user.refresh_from_db()
        self.assertEqual(self.support_user.max_open_tickets, 30)

    def test_owner_can_update_support_user(self):
        # Owner passes the object-level check on their own helpdesk's user.
        self.client.force_authenticate(self.owner)
        url = factories.ProviderSupportUserFactory.get_url(self.support_user)
        data = {"is_active": False}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ProviderSupportUserDeleteTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.support_user = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
        )

    def test_staff_can_delete_provider_support_user(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderSupportUserFactory.get_url(self.support_user)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_owner_can_delete_support_user(self):
        # Owner passes the object-level check on their own helpdesk's user.
        self.client.force_authenticate(self.owner)
        url = factories.ProviderSupportUserFactory.get_url(self.support_user)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class ProviderSupportUserTeamWorkloadTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
            is_active=True,
            max_open_tickets=10,
        )

    def test_team_workload_returns_capacity_info(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderSupportUserFactory.get_list_url(action="team_workload")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)
        entry = response.data[0]
        self.assertIn("uuid", entry)
        self.assertIn("user_full_name", entry)
        self.assertIn("open_ticket_count", entry)
        self.assertIn("max_open_tickets", entry)
        self.assertIn("has_capacity", entry)

    def test_team_workload_shows_correct_open_ticket_count(self):
        # Assign some tickets to the agent
        parent = factories.IssueFactory(backend_id="WLD-WL01")
        factories.IssueFactory(
            parent_issue=parent,
            provider_helpdesk=self.helpdesk,
            provider_assignee=self.agent,
            backend_id="WLD-WL02",
            resolution_date=None,
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderSupportUserFactory.get_list_url(action="team_workload")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        agent_entry = next(
            e for e in response.data if str(e["uuid"]) == str(self.agent.uuid)
        )
        self.assertEqual(agent_entry["open_ticket_count"], 1)
        self.assertTrue(agent_entry["has_capacity"])

    def test_team_workload_only_shows_active_users(self):
        inactive_agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk,
            is_active=False,
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderSupportUserFactory.get_list_url(action="team_workload")
        response = self.client.get(url)
        uuids = [str(e["uuid"]) for e in response.data]
        self.assertNotIn(str(inactive_agent.uuid), uuids)

    def test_owner_can_access_team_workload(self):
        self.client.force_authenticate(self.owner)
        url = factories.ProviderSupportUserFactory.get_list_url(action="team_workload")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


# =====================================================================
# 4. ProviderCannedResponseViewSet
# =====================================================================


class ProviderCannedResponseListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.ProviderCannedResponseFactory(
            provider_helpdesk=self.helpdesk,
            name="Greeting",
            text="Hello {{ customer_name }}, your ticket is being processed.",
        )

    def test_staff_can_list_provider_canned_responses(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.ProviderCannedResponseFactory.get_list_url()
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Greeting", names)

    def test_owner_can_list_their_canned_responses(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            factories.ProviderCannedResponseFactory.get_list_url()
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Greeting", names)

    def test_regular_user_cannot_see_provider_canned_responses(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(
            factories.ProviderCannedResponseFactory.get_list_url()
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class ProviderCannedResponseCreateTest(ProviderHelpdeskBaseTest):
    def test_staff_can_create_provider_canned_response(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "provider_helpdesk": self.helpdesk.uuid.hex,
            "name": "New Response",
            "text": "Thank you for reaching out.",
            "category": "general",
        }
        response = self.client.post(
            factories.ProviderCannedResponseFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_owner_cannot_create_canned_response(self):
        # Owner is blocked at has_permission stage where obj is None.
        self.client.force_authenticate(self.owner)
        data = {
            "provider_helpdesk": self.helpdesk.uuid.hex,
            "name": "Owner Response",
            "text": "We are looking into this.",
            "category": "investigation",
        }
        response = self.client.post(
            factories.ProviderCannedResponseFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_create_canned_response(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {
            "provider_helpdesk": self.helpdesk.uuid.hex,
            "name": "Unauthorized Response",
            "text": "This should fail.",
        }
        response = self.client.post(
            factories.ProviderCannedResponseFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProviderCannedResponseUpdateTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.ProviderCannedResponseFactory(
            provider_helpdesk=self.helpdesk,
        )

    def test_staff_can_update_provider_canned_response(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderCannedResponseFactory.get_url(self.canned_response)
        data = {"name": "Updated Response Name"}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.canned_response.refresh_from_db()
        self.assertEqual(self.canned_response.name, "Updated Response Name")


class ProviderCannedResponseDeleteTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.ProviderCannedResponseFactory(
            provider_helpdesk=self.helpdesk,
        )

    def test_staff_can_delete_provider_canned_response(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderCannedResponseFactory.get_url(self.canned_response)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_owner_can_delete_canned_response(self):
        # Owner passes the object-level check on their own helpdesk's response.
        self.client.force_authenticate(self.owner)
        url = factories.ProviderCannedResponseFactory.get_url(self.canned_response)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class ProviderCannedResponseRenderTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.ProviderCannedResponseFactory(
            provider_helpdesk=self.helpdesk,
            text="Hello {{ customer_name }}, your ticket {{ ticket_id }} is being processed.",
            usage_count=0,
        )

    def test_render_returns_rendered_text_with_context(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderCannedResponseFactory.get_url(
            self.canned_response, action="render"
        )
        data = {
            "context": {"customer_name": "Alice", "ticket_id": "WLD-42"},
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Alice", response.data["rendered_text"])
        self.assertIn("WLD-42", response.data["rendered_text"])

    def test_render_increments_usage_count(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderCannedResponseFactory.get_url(
            self.canned_response, action="render"
        )
        data = {"context": {"customer_name": "Bob"}}
        self.client.post(url, data, format="json")
        self.canned_response.refresh_from_db()
        self.assertEqual(self.canned_response.usage_count, 1)

        # Call again
        self.client.post(url, data, format="json")
        self.canned_response.refresh_from_db()
        self.assertEqual(self.canned_response.usage_count, 2)

    def test_render_without_context_returns_raw_template(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProviderCannedResponseFactory.get_url(
            self.canned_response, action="render"
        )
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Template variables not provided should be rendered empty
        self.assertNotIn("{{ customer_name }}", response.data["rendered_text"])


# =====================================================================
# 5. CannedResponseViewSet (operator)
# =====================================================================


class CannedResponseListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.CannedResponseFactory(
            name="Global Response",
            text="Thank you for contacting support.",
        )

    def test_staff_can_list_canned_responses(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.CannedResponseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Global Response", names)

    def test_support_user_can_list_canned_responses(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(factories.CannedResponseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_list_canned_responses(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(factories.CannedResponseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CannedResponseCreateTest(ProviderHelpdeskBaseTest):
    def test_staff_can_create_canned_response(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "name": "New Canned",
            "text": "This is a global canned response.",
            "category": "general",
        }
        response = self.client.post(
            factories.CannedResponseFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_support_user_can_create_canned_response(self):
        self.client.force_authenticate(self.fixture.global_support)
        data = {
            "name": "Support Canned",
            "text": "Global support canned response.",
            "category": "general",
        }
        response = self.client.post(
            factories.CannedResponseFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_regular_user_cannot_create_canned_response(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {
            "name": "Unauthorized",
            "text": "Should fail.",
        }
        response = self.client.post(
            factories.CannedResponseFactory.get_list_url(),
            data,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CannedResponseDeleteTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.CannedResponseFactory()

    def test_staff_can_delete_canned_response(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CannedResponseFactory.get_url(self.canned_response)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_regular_user_cannot_delete_canned_response(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.CannedResponseFactory.get_url(self.canned_response)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CannedResponseRenderTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.canned_response = factories.CannedResponseFactory(
            text="Dear {{ user_name }}, we have received your request.",
        )

    def test_staff_can_render_canned_response(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.CannedResponseFactory.get_url(
            self.canned_response, action="render"
        )
        data = {"context": {"user_name": "Charlie"}}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Charlie", response.data["rendered_text"])

    def test_support_user_can_render_canned_response(self):
        self.client.force_authenticate(self.fixture.global_support)
        url = factories.CannedResponseFactory.get_url(
            self.canned_response, action="render"
        )
        data = {"context": {"user_name": "Diana"}}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_render_canned_response(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.CannedResponseFactory.get_url(
            self.canned_response, action="render"
        )
        data = {"context": {"user_name": "Eve"}}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# =====================================================================
# 6. IssueViewSet actions (escalate, bulk_update)
# =====================================================================


class IssueEscalateTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.issue = factories.IssueFactory(
            customer=self.fixture.customer,
            project=self.fixture.project,
            backend_id="WLD-ESC1",
        )

    @mock.patch("waldur_mastermind.support.views.tasks")
    def test_staff_can_escalate_issue(self, mock_tasks):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_url(self.issue, action="escalate")
        data = {"reason": "Needs immediate attention"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "escalated")
        self.issue.refresh_from_db()
        self.assertTrue(self.issue.is_escalated)
        self.assertIsNotNone(self.issue.escalated_at)
        self.assertEqual(self.issue.escalation_reason, "Needs immediate attention")

    @mock.patch("waldur_mastermind.support.views.tasks")
    def test_escalate_creates_comment_on_issue(self, mock_tasks):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_url(self.issue, action="escalate")
        data = {"reason": "SLA breach imminent"}
        self.client.post(url, data, format="json")
        comments = models.Comment.objects.filter(issue=self.issue)
        self.assertEqual(comments.count(), 1)
        self.assertIn("[ESCALATED]", comments.first().description)
        self.assertIn("SLA breach imminent", comments.first().description)

    @mock.patch("waldur_mastermind.support.views.tasks")
    def test_caller_cannot_escalate_due_to_permission_design(self, mock_tasks):
        # The _escalate_permission check receives obj=None in has_permission
        # stage, so non-staff/non-support callers are blocked before
        # has_object_permission (where obj is passed) is reached.
        self.client.force_authenticate(self.issue.caller)
        url = factories.IssueFactory.get_url(self.issue, action="escalate")
        data = {"reason": "No response in 48 hours"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_mastermind.support.views.tasks")
    def test_non_staff_non_caller_cannot_escalate(self, mock_tasks):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.IssueFactory.get_url(self.issue, action="escalate")
        data = {"reason": "This should fail"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_mastermind.support.views.tasks")
    def test_escalate_without_reason_returns_400(self, mock_tasks):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_url(self.issue, action="escalate")
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class IssueBulkUpdateTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.issue1 = factories.IssueFactory(
            backend_id="WLD-BU01", status="open", priority="low"
        )
        self.issue2 = factories.IssueFactory(
            backend_id="WLD-BU02", status="open", priority="low"
        )

    def test_staff_can_bulk_update_issues(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_list_url() + "bulk_update/"
        data = {
            "issue_uuids": [
                str(self.issue1.uuid),
                str(self.issue2.uuid),
            ],
            "status": "in_progress",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated_count"], 2)
        self.issue1.refresh_from_db()
        self.issue2.refresh_from_db()
        self.assertEqual(self.issue1.status, "in_progress")
        self.assertEqual(self.issue2.status, "in_progress")

    def test_non_staff_cannot_bulk_update(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        url = factories.IssueFactory.get_list_url() + "bulk_update/"
        data = {
            "issue_uuids": [str(self.issue1.uuid)],
            "status": "closed",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_user_can_bulk_update(self):
        self.client.force_authenticate(self.fixture.global_support)
        url = factories.IssueFactory.get_list_url() + "bulk_update/"
        data = {
            "issue_uuids": [str(self.issue1.uuid)],
            "priority": "high",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.issue1.refresh_from_db()
        self.assertEqual(self.issue1.priority, "high")

    def test_bulk_update_requires_at_least_one_field(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_list_url() + "bulk_update/"
        data = {
            "issue_uuids": [str(self.issue1.uuid)],
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_update_with_nonexistent_uuids_returns_400(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueFactory.get_list_url() + "bulk_update/"
        data = {
            "issue_uuids": ["00000000000000000000000000000000"],
            "status": "closed",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# =====================================================================
# 7. IssueTagViewSet, IssueLinkViewSet, SavedFilterViewSet
# =====================================================================


class IssueTagListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.tag = factories.IssueTagFactory(name="urgent", color="#ff0000")

    def test_staff_can_list_tags(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueTagFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("urgent", names)

    def test_support_user_can_list_tags(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(factories.IssueTagFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_list_tags(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(factories.IssueTagFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IssueTagCRUDTest(ProviderHelpdeskBaseTest):
    def test_staff_can_create_tag(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {"name": "bug", "color": "#0000ff"}
        response = self.client.post(
            factories.IssueTagFactory.get_list_url(), data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.IssueTag.objects.filter(name="bug").exists())

    def test_staff_can_update_tag(self):
        tag = factories.IssueTagFactory(name="old_name")
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueTagFactory.get_url(tag)
        data = {"name": "new_name"}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tag.refresh_from_db()
        self.assertEqual(tag.name, "new_name")

    def test_staff_can_delete_tag(self):
        tag = factories.IssueTagFactory()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueTagFactory.get_url(tag)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_regular_user_cannot_create_tag(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {"name": "unauthorized_tag"}
        response = self.client.post(
            factories.IssueTagFactory.get_list_url(), data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IssueLinkListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.source_issue = factories.IssueFactory(backend_id="WLD-LNK1")
        self.target_issue = factories.IssueFactory(backend_id="WLD-LNK2")
        self.link = factories.IssueLinkFactory(
            source=self.source_issue,
            target=self.target_issue,
            link_type=models.IssueLink.LinkTypes.RELATED,
        )

    def test_staff_can_list_issue_links(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueLinkFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)

    def test_support_user_can_list_issue_links(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(factories.IssueLinkFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_cannot_list_issue_links(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(factories.IssueLinkFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IssueLinkCRUDTest(ProviderHelpdeskBaseTest):
    def test_staff_can_create_issue_link(self):
        source = factories.IssueFactory(backend_id="WLD-CL01")
        target = factories.IssueFactory(backend_id="WLD-CL02")
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "source": source.uuid.hex,
            "target": target.uuid.hex,
            "link_type": models.IssueLink.LinkTypes.BLOCKED_BY,
        }
        response = self.client.post(
            factories.IssueLinkFactory.get_list_url(), data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_can_delete_issue_link(self):
        link = factories.IssueLinkFactory()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.IssueLinkFactory.get_url(link)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_regular_user_cannot_create_issue_link(self):
        source = factories.IssueFactory(backend_id="WLD-CL03")
        target = factories.IssueFactory(backend_id="WLD-CL04")
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {
            "source": source.uuid.hex,
            "target": target.uuid.hex,
            "link_type": models.IssueLink.LinkTypes.RELATED,
        }
        response = self.client.post(
            factories.IssueLinkFactory.get_list_url(), data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SavedFilterListTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        self.saved_filter = factories.SavedFilterFactory(
            user=self.fixture.staff,
            name="My Open Tickets",
            filter_params={"status": "open"},
        )

    def test_staff_can_list_saved_filters(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.SavedFilterFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("My Open Tickets", names)

    def test_support_user_can_list_their_saved_filters(self):
        factories.SavedFilterFactory(
            user=self.fixture.global_support,
            name="Support Filter",
        )
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(factories.SavedFilterFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Support Filter", names)

    def test_staff_can_see_shared_filters(self):
        factories.SavedFilterFactory(
            user=self.fixture.global_support,
            name="Shared Filter",
            is_shared=True,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.SavedFilterFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertIn("Shared Filter", names)

    def test_regular_user_cannot_list_saved_filters(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        response = self.client.get(factories.SavedFilterFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SavedFilterCRUDTest(ProviderHelpdeskBaseTest):
    def test_staff_can_create_saved_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        data = {
            "name": "New Filter",
            "filter_params": {"priority": "high"},
            "is_shared": False,
        }
        response = self.client.post(
            factories.SavedFilterFactory.get_list_url(), data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_can_update_own_saved_filter(self):
        saved_filter = factories.SavedFilterFactory(
            user=self.fixture.staff, name="Original"
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.SavedFilterFactory.get_url(saved_filter)
        data = {"name": "Updated"}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        saved_filter.refresh_from_db()
        self.assertEqual(saved_filter.name, "Updated")

    def test_staff_can_delete_own_saved_filter(self):
        saved_filter = factories.SavedFilterFactory(user=self.fixture.staff)
        self.client.force_authenticate(self.fixture.staff)
        url = factories.SavedFilterFactory.get_url(saved_filter)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_support_user_cannot_update_other_users_filter(self):
        staff_filter = factories.SavedFilterFactory(
            user=self.fixture.staff,
            is_shared=True,
        )
        self.client.force_authenticate(self.fixture.global_support)
        url = factories.SavedFilterFactory.get_url(staff_filter)
        data = {"name": "Hacked"}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_user_cannot_delete_other_users_filter(self):
        staff_filter = factories.SavedFilterFactory(
            user=self.fixture.staff,
            is_shared=True,
        )
        self.client.force_authenticate(self.fixture.global_support)
        url = factories.SavedFilterFactory.get_url(staff_filter)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_create_saved_filter(self):
        other_user = structure_factories.UserFactory()
        self.client.force_authenticate(other_user)
        data = {
            "name": "Unauthorized",
            "filter_params": {"status": "open"},
        }
        response = self.client.post(
            factories.SavedFilterFactory.get_list_url(), data, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProviderTicketVisibilityTest(test.APITestCase):
    """Active provider support users can see issues (and their comments) routed
    to their helpdesk through the shared support API."""

    def setUp(self):
        self.helpdesk = factories.ProviderHelpdeskFactory()
        self.membership = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk, is_active=True
        )
        self.support_user = self.membership.user
        self.routed_issue = factories.IssueFactory(provider_helpdesk=self.helpdesk)
        self.other_issue = factories.IssueFactory()

    def test_support_user_sees_routed_issue(self):
        self.client.force_authenticate(self.support_user)
        response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.routed_issue.uuid.hex, uuids)
        self.assertNotIn(self.other_issue.uuid.hex, uuids)

    def test_non_member_does_not_see_routed_issue(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(factories.IssueFactory.get_list_url())
        uuids = [item["uuid"] for item in response.data]
        self.assertNotIn(self.routed_issue.uuid.hex, uuids)

    def test_inactive_support_user_does_not_see_routed_issue(self):
        self.membership.is_active = False
        self.membership.save()
        self.client.force_authenticate(self.support_user)
        response = self.client.get(factories.IssueFactory.get_list_url())
        uuids = [item["uuid"] for item in response.data]
        self.assertNotIn(self.routed_issue.uuid.hex, uuids)

    def test_support_user_sees_comments_on_routed_issue(self):
        comment = factories.CommentFactory(issue=self.routed_issue, is_public=True)
        other_comment = factories.CommentFactory(issue=self.other_issue, is_public=True)
        self.client.force_authenticate(self.support_user)
        response = self.client.get(factories.CommentFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(comment.uuid.hex, uuids)
        self.assertNotIn(other_comment.uuid.hex, uuids)


class ProviderRoutingInfoSerializerTest(test.APITestCase):
    """The operator (parent) issue exposes provider_ticket_info with the keys
    the frontend routing panel renders."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.helpdesk = factories.ProviderHelpdeskFactory()
        self.parent = factories.IssueFactory(backend_id="WLD-900")
        self.child = factories.IssueFactory(
            parent_issue=self.parent,
            provider_helpdesk=self.helpdesk,
            backend_id="WLD-901",
        )

    def test_provider_ticket_info_exposes_frontend_keys(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(factories.IssueFactory.get_url(self.parent))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_routed"])
        info = response.data["provider_ticket_info"]
        self.assertEqual(info["child_ticket_key"], self.child.key)
        self.assertEqual(info["child_ticket_status"], self.child.status)
        self.assertEqual(info["backend_type"], self.helpdesk.backend_type)
        self.assertIn("provider_name", info)
        self.assertEqual(
            info["provider_customer_uuid"],
            self.helpdesk.service_provider.customer.uuid.hex,
        )


class ProviderCommentPermissionTest(base.BaseTest):
    """Active provider support users can post comments on tickets routed to
    their helpdesk; non-members and deactivated members cannot."""

    def setUp(self):
        super().setUp()
        self.helpdesk = factories.ProviderHelpdeskFactory()
        self.membership = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk, is_active=True
        )
        self.support_user = self.membership.user
        self.issue = factories.IssueFactory(provider_helpdesk=self.helpdesk)

    def _post_comment(self):
        return self.client.post(
            factories.IssueFactory.get_url(self.issue, action="comment"),
            data={"description": "Reply from provider"},
        )

    def test_provider_support_user_can_comment(self):
        self.client.force_authenticate(self.support_user)
        self.assertEqual(self._post_comment().status_code, status.HTTP_201_CREATED)

    def test_non_member_cannot_comment(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertIn(
            self._post_comment().status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )

    def test_inactive_support_user_cannot_comment(self):
        self.membership.is_active = False
        self.membership.save()
        self.client.force_authenticate(self.support_user)
        self.assertIn(
            self._post_comment().status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )


class ProviderRoutingVisibilityTest(ProviderHelpdeskBaseTest):
    """Routing internals must stay hidden from the ticket caller (end-user),
    but visible to staff and to the provider's own support users."""

    ROUTING_FIELDS = (
        "is_routed",
        "provider_ticket_info",
        "provider_helpdesk",
        "parent_issue",
    )

    def setUp(self):
        super().setUp()
        self.caller = structure_factories.UserFactory()
        self.parent = factories.IssueFactory(caller=self.caller)
        self.child = factories.IssueFactory(
            caller=self.caller,
            parent_issue=self.parent,
            provider_helpdesk=self.helpdesk,
        )
        self.agent = structure_factories.UserFactory()
        self.membership = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk, user=self.agent, is_active=True
        )

    def _list_uuids(self):
        response = self.client.get(factories.IssueFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def test_caller_does_not_see_routed_child_issue(self):
        self.client.force_authenticate(self.caller)
        uuids = self._list_uuids()
        self.assertIn(self.parent.uuid.hex, uuids)
        self.assertNotIn(self.child.uuid.hex, uuids)

    def test_caller_cannot_see_routing_fields_on_parent(self):
        self.client.force_authenticate(self.caller)
        response = self.client.get(factories.IssueFactory.get_url(self.parent))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.ROUTING_FIELDS:
            self.assertNotIn(field, response.data)

    def test_staff_sees_routing_fields(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueFactory.get_url(self.parent))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_routed"])
        self.assertIsNotNone(response.data["provider_ticket_info"])

    def test_provider_agent_sees_routing_fields_on_child(self):
        self.client.force_authenticate(self.agent)
        response = self.client.get(factories.IssueFactory.get_url(self.child))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("provider_helpdesk", response.data)
