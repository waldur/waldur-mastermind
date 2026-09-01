from constance.test.unittest import override_config
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import backend
from waldur_mastermind.support.tests import factories


@override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="zammad",
    ZAMMAD_API_URL="",
    ZAMMAD_TOKEN="",
)
class UnconfiguredZammadTest(TestCase):
    """Selecting a backend without configuring it must not break reads.

    The Zammad client validates its credentials when it is constructed, and the
    issue serializer resolves the active backend for every issue it renders. A
    client built in the backend's __init__ therefore turned every issue read
    into a 500 on a deployment that had chosen Zammad but not yet filled in the
    token.
    """

    def test_resolving_the_backend_does_not_raise(self):
        self.assertIsNotNone(backend.get_active_backend())

    def test_capabilities_answer_without_a_client(self):
        active = backend.get_active_backend()
        issue = factories.IssueFactory()

        self.assertFalse(active.update_is_available(issue))
        self.assertEqual(active.get_available_statuses(issue), [])

    def test_reading_an_issue_still_works(self):
        client = test.APIClient()
        client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        issue = factories.IssueFactory()

        response = client.get(factories.IssueFactory.get_url(issue))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["available_statuses"], [])

    def test_listing_issues_still_works(self):
        client = test.APIClient()
        client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        factories.IssueFactory()

        response = client.get(factories.IssueFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
