import datetime
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.enums import ReviewStates
from waldur_core.onboarding.enums import VerificationStatus
from waldur_core.onboarding.tests.factories import OnboardingVerificationFactory
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures
from waldur_core.user_actions import providers as user_action_providers
from waldur_core.user_actions.models import UserAction
from waldur_core.user_actions.providers import DASHBOARD_LIST_LIMIT
from waldur_core.user_actions.user_actions import LegacyUserActionDashboardProvider
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


class DashboardFeedHelpersMixin:
    """Helpers for driving the dashboard feed from throwaway providers.

    Shared rather than copied: _item encodes the eight-key feed contract, so
    two copies drift the moment that contract changes.
    """

    def _register_provider(self, action_type, items):
        """Register a throwaway dashboard provider for the duration of a test."""

        class StubProvider(user_action_providers.BaseDashboardProvider):
            def get_dashboard_pending_actions(self, user):
                return items

        StubProvider.action_type = action_type
        user_action_providers._dashboard_providers[action_type] = StubProvider()
        self.addCleanup(
            user_action_providers._dashboard_providers.pop, action_type, None
        )

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


class DashboardPendingActionsTest(DashboardFeedHelpersMixin, test.APITestCase):
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


class DashboardLegacyQueueBridgeTest(DashboardFeedHelpersMixin, test.APITestCase):
    """Cover folding the persistent UserAction queue into the dashboard feed.

    The queue and the feed used to render as two separate "Pending actions"
    sections. The feed is now the only surface, so the queue reaches the user
    through LegacyUserActionDashboardProvider.
    """

    def setUp(self):
        self.url = factories.UserFactory.get_list_url("dashboard-pending-actions")
        self.fixture = fixtures.ProjectFixture()
        self.owner = self.fixture.owner
        self.client.force_authenticate(self.owner)

    def _create_action(self, user=None, **kwargs):
        # The related object is only needed to satisfy the model's mandatory
        # content_type/object_id; pointing it at the user keeps this test out
        # of the marketplace's fixtures.
        user = user or self.owner
        defaults = {
            "action_type": "test_legacy_action",
            "title": "Legacy item",
            "description": "From the queue",
            "urgency": "high",
            "content_type": ContentType.objects.get_for_model(type(user)),
            "object_id": user.id,
        }
        defaults.update(kwargs)
        return UserAction.objects.create(user=user, **defaults)

    def _legacy_items(self, response):
        return [item for item in response.data if item["uuid"] is not None]

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_queue_rows_reach_the_feed(self, mock_config):
        mock_config.USER_ACTIONS_ENABLED = True
        action = self._create_action()

        response = self.client.get(self.url)
        items = self._legacy_items(response)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["uuid"], action.uuid.hex)
        self.assertEqual(items[0]["type"], "test_legacy_action")
        self.assertTrue(items[0]["can_silence"])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_queue_is_skipped_when_disabled(self, mock_config):
        # Rows outlive the setting being turned off, so the feed has to check
        # it rather than assume an empty queue.
        mock_config.USER_ACTIONS_ENABLED = False
        self._create_action()

        response = self.client.get(self.url)
        self.assertEqual(self._legacy_items(response), [])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_silenced_rows_are_excluded(self, mock_config):
        # Silencing is the capability the feed gains by reusing the queue, so
        # it has to honour the same filter UserActionViewSet applies.
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(is_silenced=True)

        response = self.client.get(self.url)
        self.assertEqual(self._legacy_items(response), [])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_temporarily_silenced_rows_are_excluded(self, mock_config):
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(silenced_until=timezone.now() + datetime.timedelta(days=1))

        response = self.client.get(self.url)
        self.assertEqual(self._legacy_items(response), [])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_expired_silence_returns_the_row(self, mock_config):
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(silenced_until=timezone.now() - datetime.timedelta(days=1))

        response = self.client.get(self.url)
        self.assertEqual(len(self._legacy_items(response)), 1)

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_another_users_rows_are_not_leaked(self, mock_config):
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(user=factories.UserFactory())

        response = self.client.get(self.url)
        self.assertEqual(self._legacy_items(response), [])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_urgency_maps_to_variant(self, mock_config):
        # UserAction.urgency is free text and the queue's own ordering sorts it
        # alphabetically, so the mapping is explicit rather than inherited.
        mock_config.USER_ACTIONS_ENABLED = True
        for urgency, variant in (
            ("high", "error"),
            ("medium", "warning"),
            ("low", "info"),
        ):
            with self.subTest(urgency=urgency):
                UserAction.objects.all().delete()
                self._create_action(urgency=urgency)
                response = self.client.get(self.url)
                self.assertEqual(self._legacy_items(response)[0]["variant"], variant)

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_route_metadata_is_carried_through(self, mock_config):
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(
            route_name="marketplace-resource-details",
            route_params={"resource_uuid": "abc"},
        )

        item = self._legacy_items(self.client.get(self.url))[0]
        self.assertEqual(item["route_name"], "marketplace-resource-details")
        self.assertEqual(item["route_params"], {"resource_uuid": "abc"})

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_queue_rows_are_capped_by_the_provider(self, mock_config):
        # Asserting on the provider, not the response: the view caps the feed
        # too, so a response-level assertion passes even if the provider builds
        # corrective actions for the whole backlog first.
        mock_config.USER_ACTIONS_ENABLED = True
        for index in range(DASHBOARD_LIST_LIMIT + 5):
            self._create_action(
                action_type=f"test_legacy_action_{index}", object_id=self.owner.id
            )

        provider = LegacyUserActionDashboardProvider()
        self.assertEqual(
            len(provider.get_dashboard_pending_actions(self.owner)),
            DASHBOARD_LIST_LIMIT,
        )

    def test_computed_items_expose_the_new_fields(self):
        # Providers written before the bridge return only the original eight
        # keys; the dispatcher fills the rest so they need no changes.
        self._register_provider(
            "test_computed", [self._item("test_computed", "info", "computed")]
        )

        item = self.client.get(self.url).data[0]
        self.assertIsNone(item["uuid"])
        self.assertIsNone(item["urgency"])
        self.assertIsNone(item["route_name"])
        self.assertEqual(item["route_params"], {})
        self.assertFalse(item["can_silence"])
        self.assertEqual(item["actions"], [])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_corrective_action_flags_serialise_as_booleans(self, mock_config):
        # api_endpoint decides whether the frontend executes the action or just
        # navigates. Serialised through a CharField it renders False as the
        # string "False", which is truthy in JS, so every navigation action
        # executed instead — including "View Resource".
        mock_config.USER_ACTIONS_ENABLED = True
        action = self._create_action(action_type="test_corrective")

        class ProviderWithActions(user_action_providers.BaseActionProvider):
            action_type = "test_corrective"

            def get_actions_for_user(self, user):
                return []

            def get_affected_users(self):
                return []

            def get_corrective_actions(self, user, obj):
                return [
                    user_action_providers.CorrectiveAction(
                        label="View Thing",
                        category=user_action_providers.ActionCategory.VIEW,
                        route_name="thing-details",
                    )
                ]

        user_action_providers._providers["test_corrective"] = ProviderWithActions()
        self.addCleanup(user_action_providers._providers.pop, "test_corrective", None)

        item = self._legacy_items(self.client.get(self.url))[0]
        self.assertEqual(item["uuid"], action.uuid.hex)
        corrective = item["actions"][0]
        self.assertIs(corrective["api_endpoint"], False)
        self.assertIs(corrective["confirmation_required"], False)

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_uuids_render_in_the_same_form_as_computed_items(self, mock_config):
        # UserAction keeps these on plain UUIDFields, which render hyphenated,
        # while every other provider passes a StringUUID and renders bare hex.
        # A frontend deep-linking off target_uuid needs one answer, and hex is
        # the form Waldur routes on.
        mock_config.USER_ACTIONS_ENABLED = True
        customer = self.fixture.customer
        self._create_action(
            organization_uuid=customer.uuid, resource_uuid=customer.uuid
        )

        item = self._legacy_items(self.client.get(self.url))[0]
        self.assertEqual(item["customer_uuid"], customer.uuid.hex)
        self.assertEqual(item["target_uuid"], customer.uuid.hex)
        self.assertNotIn("-", item["customer_uuid"])

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_row_whose_target_is_gone_does_not_hide_the_queue(self, mock_config):
        # Providers dereference the related object unguarded and the dispatcher
        # drops a whole provider when one raises, so a single row left behind
        # by a deleted object would take every queued action down with it.
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(action_type="test_survivor")
        doomed = self._create_action(action_type="test_orphan")
        UserAction.objects.filter(pk=doomed.pk).update(object_id=0)

        class ProviderThatDerefs(user_action_providers.BaseActionProvider):
            action_type = "test_orphan"

            def get_actions_for_user(self, user):
                return []

            def get_affected_users(self):
                return []

            def get_corrective_actions(self, user, obj):
                return [
                    user_action_providers.CorrectiveAction(
                        label=str(obj.uuid),
                        category=user_action_providers.ActionCategory.VIEW,
                    )
                ]

        user_action_providers._providers["test_orphan"] = ProviderThatDerefs()
        self.addCleanup(user_action_providers._providers.pop, "test_orphan", None)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        types = [item["type"] for item in self._legacy_items(response)]
        self.assertIn("test_survivor", types)

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_query_count_does_not_grow_with_queue_rows(self, mock_config):
        # The feed is on the page-load path, and resolving each row's
        # GenericForeignKey is a query select_related cannot span, so without
        # the prefetch this costs a query per row. Measured on the provider
        # rather than the endpoint: the other providers do their own reads,
        # which would drown the signal this is guarding.
        mock_config.USER_ACTIONS_ENABLED = True
        provider = LegacyUserActionDashboardProvider()

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                provider.get_dashboard_pending_actions(self.owner)
            return len(ctx.captured_queries)

        self._create_action(action_type="test_row_0")
        one_row = count_queries()

        for index in range(1, DASHBOARD_LIST_LIMIT):
            self._create_action(action_type=f"test_row_{index}")

        self.assertEqual(count_queries(), one_row)

    @mock.patch("waldur_core.user_actions.user_actions.config")
    def test_dated_items_sort_before_undated_of_equal_severity(self, mock_config):
        # The queue ordered on due_date and the feed had no tiebreak at all, so
        # merging them without one interleaves the two sources arbitrarily.
        mock_config.USER_ACTIONS_ENABLED = True
        self._create_action(action_type="test_undated", urgency="high", due_date=None)
        self._create_action(
            action_type="test_dated",
            urgency="high",
            due_date=timezone.now() + datetime.timedelta(days=1),
        )

        types = [item["type"] for item in self._legacy_items(self.client.get(self.url))]
        self.assertEqual(types, ["test_dated", "test_undated"])
