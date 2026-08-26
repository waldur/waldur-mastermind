from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.core.enums import ReviewStates
from waldur_core.onboarding.enums import VerificationStatus
from waldur_core.onboarding.tests.factories import OnboardingVerificationFactory
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures
from waldur_core.user_actions.providers import DASHBOARD_LIST_LIMIT
from waldur_core.users.enums import InvitationState
from waldur_core.users.tests.factories import (
    CustomerGroupInvitationFactory,
    CustomerInvitationFactory,
    PermissionRequestFactory,
    ProjectGroupInvitationFactory,
)


class DashboardGeneralStatsTest(test.APITestCase):
    def setUp(self):
        self.url = factories.UserFactory.get_list_url("dashboard-general-stats")
        self.fixture = fixtures.CustomerFixture()
        self.owner = self.fixture.owner
        self.customer = self.fixture.customer
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_zero_counts_for_user_with_no_activity(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["pending_permission_requests"], 0)
        self.assertEqual(response.data["active_invitations"], 0)
        self.assertEqual(response.data["pending_onboarding_applications"], 0)

    def test_counts_pending_permission_requests_in_managed_customers(self):
        # An owner of customer can act on permission requests for that customer
        group_invitation = CustomerGroupInvitationFactory(scope=self.customer)
        PermissionRequestFactory(
            invitation=group_invitation, state=ReviewStates.PENDING
        )
        PermissionRequestFactory(
            invitation=group_invitation, state=ReviewStates.PENDING
        )
        # Other customer should not be counted
        PermissionRequestFactory(state=ReviewStates.PENDING)
        # Approved one should not be counted
        PermissionRequestFactory(
            invitation=group_invitation, state=ReviewStates.APPROVED
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending_permission_requests"], 2)

    def test_counts_active_invitations_addressed_to_user(self):
        CustomerInvitationFactory(email=self.owner.email, state=InvitationState.PENDING)
        CustomerInvitationFactory(email=self.owner.email, state=InvitationState.PENDING)
        # Accepted should not count
        CustomerInvitationFactory(
            email=self.owner.email, state=InvitationState.ACCEPTED
        )
        # Other user's invitation should not count
        CustomerInvitationFactory(state=InvitationState.PENDING)

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["active_invitations"], 2)

    def test_invitations_are_not_matched_on_a_missing_civil_number(self):
        # User.civil_number is nullable and Invitation.civil_number is NOT NULL
        # and blank for "anyone may accept", so a naive equality arm compiles to
        # `civil_number IS NULL` — and a user carrying no identifier at all must
        # not be handed every pending invitation on the platform.
        self.owner.civil_number = None
        self.owner.email = ""
        self.owner.save()
        CustomerInvitationFactory(state=InvitationState.PENDING, civil_number="")
        CustomerInvitationFactory(state=InvitationState.PENDING, civil_number="X-1")

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["active_invitations"], 0)

    def _count_stats_queries(self) -> int:
        self.client.force_authenticate(self.owner)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url)
        return len(ctx.captured_queries)

    def test_query_count_does_not_grow_with_pending_requests(self):
        # can_manage_permission_request walks scope -> project -> customer ->
        # call organiser, so before the role index each candidate cost up to
        # four permission queries — on an endpoint hit on every page load.
        for _ in range(5):
            PermissionRequestFactory(
                invitation=CustomerGroupInvitationFactory(scope=self.customer),
                state=ReviewStates.PENDING,
            )
        small_count = self._count_stats_queries()

        for _ in range(45):
            PermissionRequestFactory(
                invitation=CustomerGroupInvitationFactory(scope=self.customer),
                state=ReviewStates.PENDING,
            )
        large_count = self._count_stats_queries()

        self.assertEqual(self._get_stats()["pending_permission_requests"], 50)
        self.assertLess(
            large_count,
            small_count + 5,
            f"Query count grew from {small_count} to {large_count} when pending "
            f"requests went from 5 to 50 — likely a per-candidate N+1 regression.",
        )

    def _get_stats(self):
        self.client.force_authenticate(self.owner)
        return self.client.get(self.url).data

    def test_counts_pending_permission_requests_on_project_scoped_invitations(self):
        # A project role holding PROJECT.CREATE_PERMISSION can approve a
        # project-scoped group invitation without holding any customer role,
        # so scoping the badge by a customer permission alone hid it from one
        # of the people who can actually act on it.
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        fixture = fixtures.ProjectFixture()
        group_invitation = ProjectGroupInvitationFactory(scope=fixture.project)
        PermissionRequestFactory(
            invitation=group_invitation, state=ReviewStates.PENDING
        )

        self.client.force_authenticate(fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending_permission_requests"], 1)

    def test_counts_auto_create_project_requests_by_project_creation_authority(self):
        # PermissionRequest.approve creates a project and grants a role on it,
        # so can_manage_permission_request short-circuits these to
        # CREATE_PROJECT_PERMISSION on the customer — not
        # CREATE_CUSTOMER_PERMISSION.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        group_invitation = CustomerGroupInvitationFactory(
            scope=self.customer, auto_create_project=True
        )
        PermissionRequestFactory(
            invitation=group_invitation, state=ReviewStates.PENDING
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending_permission_requests"], 1)

    def test_skips_auto_create_project_requests_without_project_authority(self):
        # The mirror image: CREATE_CUSTOMER_PERMISSION alone used to badge a
        # request the API answers with a 404.
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        group_invitation = CustomerGroupInvitationFactory(
            scope=self.customer, auto_create_project=True
        )
        PermissionRequestFactory(
            invitation=group_invitation, state=ReviewStates.PENDING
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending_permission_requests"], 0)

    def test_skips_invitations_that_cannot_be_accepted_yet(self):
        # PENDING_PROJECT means the project start date has not arrived and the
        # invitation has not been sent; Invitation.accept answers it with a 404,
        # so counting it produced a badge the user could not clear.
        CustomerInvitationFactory(
            email=self.owner.email, state=InvitationState.PENDING_PROJECT
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["active_invitations"], 0)

    def test_counts_invitations_matched_by_civil_number(self):
        self.owner.civil_number = "31415926535"
        self.owner.save()
        CustomerInvitationFactory(
            email="someone.else@example.com",
            civil_number=self.owner.civil_number,
            state=InvitationState.PENDING,
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["active_invitations"], 1)

    def test_skips_invitations_addressed_to_somebody_else(self):
        # filter_pending_invitations is the accept-time authorisation gate: its
        # Q(civil_number="") arm matches every blank-civil-number invitation on
        # the platform, which is not a recipient predicate.
        CustomerInvitationFactory(
            email="someone.else@example.com",
            civil_number="",
            state=InvitationState.PENDING,
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["active_invitations"], 0)

    def test_counts_pending_onboarding_applications_owned_by_user(self):
        OnboardingVerificationFactory(
            user=self.owner, status=VerificationStatus.PENDING
        )
        OnboardingVerificationFactory(
            user=self.owner, status=VerificationStatus.PENDING
        )
        # Verified should not count
        OnboardingVerificationFactory(
            user=self.owner, status=VerificationStatus.VERIFIED
        )
        # Other user's onboarding should not count
        OnboardingVerificationFactory(status=VerificationStatus.PENDING)

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data["pending_onboarding_applications"], 2)

    def test_response_schema_contains_expected_keys(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(response.data.keys()),
            {
                "pending_permission_requests",
                "active_invitations",
                "pending_onboarding_applications",
            },
        )


class DashboardPendingActionsTest(test.APITestCase):
    """Cover the dispatcher and the one provider that lives in waldur_core.

    Provider-specific behaviour for marketplace orders, erred resources,
    Terms of Service and overdue invoices is covered in their owning apps'
    test modules.
    """

    def setUp(self):
        self.url = factories.UserFactory.get_list_url("dashboard-pending-actions")
        self.fixture = fixtures.ProjectFixture()
        self.owner = self.fixture.owner

    def test_anonymous_user_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_empty_feed_when_no_actions(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(list(response.data), [])

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_emits_profile_incomplete_when_attributes_missing(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        self.owner.phone_number = ""
        self.owner.save()

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        items = [item for item in response.data if item["type"] == "profile_incomplete"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["variant"], "info")
        self.assertIn("phone_number", items[0]["description"])

    @mock.patch("waldur_core.core.user_attributes.config")
    def test_skips_profile_item_when_complete(self, mock_config):
        mock_config.MANDATORY_USER_ATTRIBUTES = ["phone_number"]
        self.owner.phone_number = "+1234567890"
        self.owner.save()

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        items = [item for item in response.data if item["type"] == "profile_incomplete"]
        self.assertEqual(items, [])

    def test_feed_is_capped(self):
        # No provider is trusted to bound itself: the assembled feed is capped
        # so one chatty provider cannot make every dashboard load enormous.
        from waldur_core.user_actions import providers

        class ChattyProvider(providers.BaseDashboardProvider):
            action_type = "test_chatty_provider"

            def get_dashboard_pending_actions(self, user):
                return [
                    {
                        "type": "test_chatty_provider",
                        "title": f"item {index}",
                        "description": "",
                        "variant": "info",
                        "deadline": None,
                        "count": None,
                        "target_uuid": None,
                        "customer_uuid": None,
                    }
                    for index in range(DASHBOARD_LIST_LIMIT + 5)
                ]

        providers._dashboard_providers["test_chatty_provider"] = ChattyProvider()
        try:
            self.client.force_authenticate(self.owner)
            response = self.client.get(self.url)
            self.assertEqual(len(response.data), DASHBOARD_LIST_LIMIT)
        finally:
            providers._dashboard_providers.pop("test_chatty_provider", None)

    def _register_provider(self, action_type, items):
        """Register a throwaway dashboard provider for the duration of a test."""
        from waldur_core.user_actions import providers

        class StubProvider(providers.BaseDashboardProvider):
            def get_dashboard_pending_actions(self, user):
                return items

        StubProvider.action_type = action_type
        providers._dashboard_providers[action_type] = StubProvider()
        self.addCleanup(providers._dashboard_providers.pop, action_type, None)

    @staticmethod
    def _item(action_type, variant, title):
        return {
            "type": action_type,
            "title": title,
            "description": "",
            "variant": variant,
            "deadline": None,
            "count": None,
            "target_uuid": None,
            "customer_uuid": None,
        }

    def test_severity_survives_the_cap(self):
        # What gets truncated must not depend on provider registration order —
        # that is whatever order apps.get_app_configs() yields, so an overdue
        # invoice would drop off the end the moment an extension moved in
        # INSTALLED_APPS.
        self._register_provider(
            "test_noisy",
            [
                self._item("test_noisy", "info", f"item {index}")
                for index in range(DASHBOARD_LIST_LIMIT)
            ],
        )
        self._register_provider(
            "test_urgent", [self._item("test_urgent", "error", "urgent")]
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.data[0]["type"], "test_urgent")

    def test_dispatcher_isolates_failing_providers(self):
        # If one provider raises, the dispatcher should log and continue with
        # the others so a buggy plugin doesn't take the whole dashboard down.
        from waldur_core.user_actions import providers

        class FailingProvider(providers.BaseDashboardProvider):
            action_type = "test_failing_provider"

            def get_dashboard_pending_actions(self, user):
                raise RuntimeError("boom")

        providers._dashboard_providers["test_failing_provider"] = FailingProvider()
        try:
            self.client.force_authenticate(self.owner)
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
        finally:
            providers._dashboard_providers.pop("test_failing_provider", None)

    def test_dashboard_providers_stay_out_of_the_user_action_queue(self):
        # They are computed live; joining the UserAction registry would only
        # buy no-op celery fanout and phantom UserActionProvider rows.
        from waldur_core.user_actions import providers

        overlap = set(providers.get_all_dashboard_providers()) & set(
            providers.get_all_providers()
        )
        self.assertEqual(overlap, set())
