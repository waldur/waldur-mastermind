from unittest import mock

from django.contrib.contenttypes.models import ContentType
from rest_framework import status

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import factories
from waldur_mastermind.support.tests.test_provider_api import ProviderHelpdeskBaseTest


class RerouteActionTest(ProviderHelpdeskBaseTest):
    """Tests for the IssueViewSet.reroute action (move an already-routed issue)."""

    def setUp(self):
        super().setUp()
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.resource = marketplace_factories.ResourceFactory(offering=self.offering)
        ct = ContentType.objects.get_for_model(self.resource)
        self.issue = factories.IssueFactory(
            resource_content_type=ct,
            resource_object_id=self.resource.id,
            backend_id="WLD-600",
            customer=self.customer,
        )
        # A different provider to reroute to.
        self.other_helpdesk = factories.ProviderHelpdeskFactory()

    def _url(self, issue=None):
        return factories.IssueFactory.get_url(issue or self.issue, action="reroute")

    def _route_to(self, helpdesk):
        return factories.IssueFactory(
            parent_issue=self.issue,
            provider_helpdesk=helpdesk,
            backend_id="WLD-601",
        )

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_staff_can_reroute_to_another_helpdesk(self, mock_get_backend):
        mock_backend = mock.Mock()
        mock_get_backend.return_value = mock_backend
        old_child = self._route_to(self.helpdesk)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.other_helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Old child removed, one fresh child pointing at the new helpdesk.
        self.assertFalse(models.Issue.objects.filter(id=old_child.id).exists())
        children = models.Issue.objects.filter(parent_issue=self.issue)
        self.assertEqual(children.count(), 1)
        self.assertEqual(children.first().provider_helpdesk, self.other_helpdesk)
        # Old ticket torn down, new ticket created on the provider backend.
        mock_backend.delete_issue.assert_called_once()
        mock_backend.create_issue.assert_called_once()

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_reroute_unrouted_issue_returns_400(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.other_helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_get_backend.assert_not_called()

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_reroute_to_same_provider_returns_400(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()
        self._route_to(self.helpdesk)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_get_backend.assert_not_called()

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_regular_user_cannot_reroute(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()
        old_child = self._route_to(self.helpdesk)
        other_user = structure_factories.UserFactory()

        self.client.force_authenticate(other_user)
        response = self.client.post(
            self._url(),
            {"provider_helpdesk": self.other_helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Nothing torn down or created.
        self.assertTrue(models.Issue.objects.filter(id=old_child.id).exists())
        self.assertEqual(
            models.Issue.objects.filter(parent_issue=self.issue).count(), 1
        )
