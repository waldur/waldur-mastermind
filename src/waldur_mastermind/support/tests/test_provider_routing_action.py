import uuid
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import factories
from waldur_mastermind.support.tests.test_provider_api import ProviderHelpdeskBaseTest


class RouteToProviderActionTest(ProviderHelpdeskBaseTest):
    """Tests for the IssueViewSet.route_to_provider manual routing action."""

    def setUp(self):
        super().setUp()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.resource = marketplace_factories.ResourceFactory(offering=self.offering)
        ct = ContentType.objects.get_for_model(self.resource)
        self.issue = factories.IssueFactory(
            resource_content_type=ct,
            resource_object_id=self.resource.id,
            backend_id="WLD-500",
            customer=self.customer,
        )

    def _url(self, issue=None):
        return factories.IssueFactory.get_url(
            issue or self.issue, action="route_to_provider"
        )

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_staff_can_route_unrouted_issue_to_chosen_helpdesk(self, mock_get_backend):
        mock_backend = mock.Mock()
        mock_get_backend.return_value = mock_backend

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        child_issue = models.Issue.objects.filter(parent_issue=self.issue).first()
        self.assertIsNotNone(child_issue)
        self.assertEqual(child_issue.provider_helpdesk, self.helpdesk)
        self.assertEqual(child_issue.parent_issue, self.issue)
        mock_backend.create_issue.assert_called_once_with(child_issue)

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_routing_already_routed_issue_returns_400(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()
        factories.IssueFactory(parent_issue=self.issue, backend_id="WLD-501")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_get_backend.assert_not_called()

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_regular_user_cannot_route_issue(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()
        other_user = structure_factories.UserFactory()

        self.client.force_authenticate(other_user)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Issue.objects.filter(parent_issue=self.issue).exists())

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_inactive_helpdesk_is_rejected_by_serializer(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()
        self.helpdesk.is_active = False
        self.helpdesk.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Issue.objects.filter(parent_issue=self.issue).exists())

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_unknown_helpdesk_uuid_is_rejected(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": uuid.uuid4().hex},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Issue.objects.filter(parent_issue=self.issue).exists())
