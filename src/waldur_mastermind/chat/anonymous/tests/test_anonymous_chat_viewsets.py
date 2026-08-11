"""Tests for the staff/support read ViewSets exposing anonymous
chat transcripts and feedback rows.

Risk profile (per the backend plan): a permission-gating slip silently
leaks every anonymous transcript to every authenticated user. So the
gating tests are explicit and duplicated across two layers
(``permission_classes`` and ``get_queryset``).
"""

import datetime
import inspect

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone as django_timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.anonymous import views as anonymous_views


def _make_interaction(**overrides):
    defaults = dict(
        user_input="hi",
        ip_address="1.2.3.4",
        session_id="session-abc",
        user_slug="abc123",
        offering_uuids=[],
        is_flagged=False,
    )
    defaults.update(overrides)
    return anonymous_models.AnonymousChatInteraction.objects.create(**defaults)


def _make_feedback(interaction, **overrides):
    defaults = dict(score=1)
    defaults.update(overrides)
    return anonymous_models.AnonymousChatFeedback.objects.create(
        interaction=interaction, **defaults
    )


class InteractionPermissionTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-list")
        self.interaction = _make_interaction()

    def test_anonymous_returns_401(self):
        # IsAuthenticated rejects unauthenticated callers — never 403.
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_end_user_returns_403(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_returns_200(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_returns_200(self):
        support = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(user=support)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_get_queryset_returns_none_for_non_staff(self):
        # Defense-in-depth: if a future action drops _permissions, the
        # queryset alone must not leak rows.
        user = structure_factories.UserFactory()
        viewset = anonymous_views.AnonymousChatInteractionViewSet()
        viewset.request = type("Req", (), {"user": user})()
        self.assertEqual(viewset.get_queryset().count(), 0)

    def test_get_queryset_returns_rows_for_staff(self):
        staff = structure_factories.UserFactory(is_staff=True)
        viewset = anonymous_views.AnonymousChatInteractionViewSet()
        viewset.request = type("Req", (), {"user": staff})()
        self.assertEqual(viewset.get_queryset().count(), 1)


class FeedbackPermissionTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("anonymous-chat-feedback-list")
        interaction = _make_interaction()
        _make_feedback(interaction, score=-1, category="inaccurate")

    def test_anonymous_returns_401(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_end_user_returns_403(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_returns_200(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ActionIntrospectionTest(SimpleTestCase):
    """Walks every @action plus standard CRUD methods on each ViewSet
    and asserts a ``<name>_permissions`` attribute exists. This is the
    guard that catches a future developer adding ``@action def
    export_csv`` without ``export_csv_permissions`` — the test fails
    rather than silently shipping an open endpoint.
    """

    VIEWSETS = (
        anonymous_views.AnonymousChatInteractionViewSet,
        anonymous_views.AnonymousChatFeedbackViewSet,
    )

    STANDARD_ACTIONS = ("list", "retrieve")  # http_method_names = ["get"]

    def _custom_actions(self, viewset):
        names = []
        for name, method in inspect.getmembers(viewset, predicate=inspect.isfunction):
            if getattr(method, "detail", None) is None:
                continue
            # Filter to drf_action-decorated methods. The ``@action``
            # decorator marks the method; presence of the ``detail``
            # attribute is the canonical signal.
            names.append(name)
        return names

    def test_every_action_has_permissions_list(self):
        for viewset in self.VIEWSETS:
            for action_name in (
                *self.STANDARD_ACTIONS,
                *self._custom_actions(viewset),
            ):
                attr = f"{action_name}_permissions"
                with self.subTest(viewset=viewset.__name__, action=action_name):
                    self.assertTrue(
                        hasattr(viewset, attr),
                        f"{viewset.__name__}.{action_name} is missing {attr}",
                    )
                    perms = getattr(viewset, attr)
                    self.assertTrue(
                        perms,
                        f"{viewset.__name__}.{attr} is empty — every action must list at least one permission",
                    )

    def test_every_action_includes_staff_or_support_check(self):
        from waldur_core.structure.permissions import is_staff_or_support

        for viewset in self.VIEWSETS:
            for action_name in (
                *self.STANDARD_ACTIONS,
                *self._custom_actions(viewset),
            ):
                with self.subTest(viewset=viewset.__name__, action=action_name):
                    attr = f"{action_name}_permissions"
                    perms = getattr(viewset, attr, [])
                    self.assertIn(is_staff_or_support, perms)


class StaffListAndActionsTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)
        self.list_url = reverse("anonymous-chat-interaction-list")
        self.kpi_url = reverse("anonymous-chat-interaction-kpi")
        self.session_url = reverse(
            "anonymous-chat-interaction-by-session", args=["session-A"]
        )
        self.user_url = reverse("anonymous-chat-interaction-by-user", args=["slug-X"])
        self.conversations_url = reverse("anonymous-chat-interaction-conversations")

        self.s1_a = _make_interaction(
            session_id="session-A",
            user_slug="slug-X",
            input_tokens=10,
            output_tokens=20,
        )
        self.s1_b = _make_interaction(
            session_id="session-A",
            user_slug="slug-X",
            input_tokens=5,
            output_tokens=7,
        )
        self.s2 = _make_interaction(
            session_id="session-B",
            user_slug="slug-X",
            is_flagged=True,
            severity="high",
            input_tokens=100,
            output_tokens=1,
        )
        # Deliberately left without token columns — stands in for turns recorded
        # before per-interaction token capture existed.
        self.other_user = _make_interaction(session_id="session-C", user_slug="slug-Y")

        _make_feedback(self.s1_a, score=1)
        _make_feedback(self.s2, score=-1, category="inaccurate")

    def _rows(self, response):
        # The list endpoint can return either a paginated dict or a
        # plain list depending on whether pagination is configured at
        # the project level. Normalise here so the tests don't care.
        data = response.data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def test_list_returns_all_interactions(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(self._rows(response)), 4)

    def test_filter_by_is_flagged(self):
        response = self.client.get(self.list_url, {"is_flagged": "true"})
        self.assertEqual(len(self._rows(response)), 1)

    def test_filter_has_feedback(self):
        # Any rating, not just negative ones: the fixture has one thumbs-up and
        # one thumbs-down, and both count as rated.
        response = self.client.get(self.list_url, {"has_feedback": "true"})
        self.assertEqual(len(self._rows(response)), 2)

    def test_filter_has_feedback_false_excludes_rated_turns(self):
        rated = self.client.get(self.list_url, {"has_feedback": "true"})
        unrated = self.client.get(self.list_url, {"has_feedback": "false"})
        total = self.client.get(self.list_url)

        self.assertEqual(
            len(self._rows(rated)) + len(self._rows(unrated)),
            len(self._rows(total)),
        )

    def test_by_session_returns_ordered_turns(self):
        response = self.client.get(self.session_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Ordered chronologically — s1_a before s1_b
        self.assertEqual(response.data[0]["uuid"], str(self.s1_a.uuid))

    def test_by_session_reports_click_count_without_skewing_conversations(self):
        anonymous_models.AnonymousChatClick.objects.create(
            interaction=self.s1_a,
            offering_uuid="11111111-1111-1111-1111-111111111111",
        )
        anonymous_models.AnonymousChatClick.objects.create(
            interaction=self.s1_a,
            offering_uuid="22222222-2222-2222-2222-222222222222",
        )

        turns = {row["uuid"]: row for row in self.client.get(self.session_url).data}
        self.assertEqual(turns[str(self.s1_a.uuid)]["click_count"], 2)
        self.assertEqual(turns[str(self.s1_b.uuid)]["click_count"], 0)

        # The clicks join must stay inside by_session — conversations groups over
        # the same base queryset and would otherwise count each turn twice.
        rows = {
            row["session_id"]: row
            for row in self.client.get(self.conversations_url).data
        }
        self.assertEqual(rows["session-A"]["message_count"], 2)

    def test_by_user_groups_across_sessions(self):
        response = self.client.get(self.user_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # slug-X owns session-A (2 interactions) + session-B (1) = 3
        self.assertEqual(len(response.data), 3)

    def test_conversations_groups_by_session(self):
        response = self.client.get(self.conversations_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {row["session_id"]: row for row in response.data}
        # One row per conversation — sessions A, B, C.
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["session-A"]["message_count"], 2)
        self.assertTrue(rows["session-A"]["has_feedback"])
        self.assertFalse(rows["session-A"]["is_flagged"])
        self.assertEqual(rows["session-A"]["max_severity"], "none")
        self.assertTrue(rows["session-B"]["is_flagged"])
        self.assertEqual(rows["session-B"]["max_severity"], "high")

    def test_kpi_aggregates(self):
        response = self.client.get(self.kpi_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.data
        self.assertEqual(body["interactions_total"], 4)
        self.assertEqual(body["sessions_total"], 3)
        self.assertEqual(body["unique_users"], 2)
        self.assertEqual(body["flagged_total"], 1)
        self.assertEqual(body["feedback_positive"], 1)
        self.assertEqual(body["feedback_negative"], 1)
        # Satisfaction rate = 1 / (1 + 1) = 0.5
        self.assertAlmostEqual(body["satisfaction_rate"], 0.5)
        # No clicks recorded → CTR is 0/4 = 0.0
        self.assertEqual(body["clicks_total"], 0)
        self.assertAlmostEqual(body["click_through_rate"], 0.0)

    def test_kpi_sums_tokens_skipping_unrecorded_turns(self):
        body = self.client.get(self.kpi_url).data
        # other_user carries NULL columns; Sum has to skip it rather than
        # nulling the whole roll-up, so the totals cover s1_a + s1_b + s2.
        self.assertEqual(body["input_tokens_total"], 115)
        self.assertEqual(body["output_tokens_total"], 28)

    def test_kpi_token_totals_are_zero_without_interactions(self):
        anonymous_models.AnonymousChatInteraction.objects.all().delete()
        body = self.client.get(self.kpi_url).data
        # Sum over an empty set is NULL — the tile needs a number, not None.
        self.assertEqual(body["input_tokens_total"], 0)
        self.assertEqual(body["output_tokens_total"], 0)

    def test_kpi_returns_403_for_non_staff(self):
        # Switch to a non-staff user; the kpi action must not return 200
        # with empty data (which would be the "silent leak" failure).
        end_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=end_user)
        response = self.client.get(self.kpi_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ActionDecoratorPresenceTest(SimpleTestCase):
    """Quick sanity: the three @action methods on the interaction
    ViewSet are actually registered as DRF actions (not plain methods).
    Catches a missed decorator that would silently revert the URL to
    the standard list/retrieve set.
    """

    def test_actions_decorated(self):
        for name in ("by_session", "by_user_detail", "by_user_list", "kpi"):
            method = getattr(anonymous_views.AnonymousChatInteractionViewSet, name)
            self.assertTrue(
                hasattr(method, "detail"),
                f"{name} is missing @action decorator",
            )


class AnonymousInteractionCreatedRangeFilterTest(test.APITestCase):
    """The date range params are named to match the rest of the codebase
    (`created_after`/`created_before`), so one frontend period helper can
    drive both this tab and the authenticated one.
    """

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-kpi")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)

    def _interaction_on(self, year, month, day):
        interaction = _make_interaction(session_id=f"session-{year}-{month}-{day}")
        # Late in the day on purpose: a `lte` lookup against a bare date would
        # resolve to midnight and drop this row.
        anonymous_models.AnonymousChatInteraction.objects.filter(
            pk=interaction.pk
        ).update(
            created=datetime.datetime(year, month, day, 23, 30, tzinfo=datetime.UTC)
        )
        return interaction

    def test_created_after_before_are_inclusive(self):
        self._interaction_on(2026, 6, 30)
        self._interaction_on(2026, 7, 1)
        self._interaction_on(2026, 7, 31)

        response = self.client.get(
            self.url, {"created_after": "2026-07-01", "created_before": "2026-07-31"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["interactions_total"], 2)


class ConversationsExposeAggregateFieldsTest(test.APITestCase):
    """Aggregate-level tests pass even when a field never reaches the
    serializer, so the API contract is asserted here rather than only against
    ``session_aggregates``.
    """

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-conversations")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)
        _make_interaction(model="test-model")

    def test_serializes_models_used_and_review_state(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        self.assertEqual(row["models_used"], "test-model")
        self.assertFalse(row["is_reviewed"])

    def test_filters_by_review_state(self):
        # The judge stamps one verdict per conversation, so the filter is a
        # partition: scored conversations and untouched ones.
        response = self.client.get(self.url, {"is_reviewed": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data)
        self.assertFalse(any(row["is_reviewed"] for row in response.data))

        response = self.client.get(self.url, {"is_reviewed": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


class ConversationTokenRollupTest(test.APITestCase):
    """Per-conversation token spend, mirroring the authenticated thread table."""

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-conversations")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)

    def test_sums_tokens_across_the_conversation(self):
        _make_interaction(session_id="s1", input_tokens=100, output_tokens=10)
        _make_interaction(session_id="s1", input_tokens=300, output_tokens=30)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [row] = response.data
        self.assertEqual(row["input_tokens"], 400)
        self.assertEqual(row["output_tokens"], 40)
        self.assertEqual(row["total_tokens"], 440)

    def test_untracked_turns_read_as_zero_not_null(self):
        # Tokens are nullable on the interaction; a null sum would render an
        # empty cell where the auth table shows a number.
        _make_interaction(session_id="s1")

        response = self.client.get(self.url)

        [row] = response.data
        self.assertEqual(row["input_tokens"], 0)
        self.assertEqual(row["output_tokens"], 0)
        self.assertEqual(row["total_tokens"], 0)


class ConversationRangeFilterTest(test.APITestCase):
    """The range filters must select whole conversations.

    Applied to interactions they would instead drop individual turns and
    re-aggregate the survivors, silently changing message_count and every
    other column on a partially-matching row.
    """

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-conversations")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)
        _make_interaction(session_id="s1", input_tokens=100, output_tokens=0)
        _make_interaction(session_id="s1", input_tokens=900, output_tokens=0)

    def test_matching_conversation_is_returned_whole(self):
        response = self.client.get(self.url, {"total_tokens_min": 500})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [row] = response.data
        # Both turns, not just the 900 one.
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(row["total_tokens"], 1000)

    def test_conversation_below_the_floor_drops_out_entirely(self):
        response = self.client.get(self.url, {"total_tokens_min": 1500})

        self.assertEqual(response.data, [])

    def test_input_and_output_ranges_narrow_independently(self):
        self.assertEqual(
            len(self.client.get(self.url, {"input_tokens_min": 1001}).data), 0
        )
        self.assertEqual(
            len(self.client.get(self.url, {"input_tokens_max": 1000}).data), 1
        )
        self.assertEqual(
            len(self.client.get(self.url, {"output_tokens_max": 0}).data), 1
        )


class ConversationLastActiveFilterTest(test.APITestCase):
    """``last_active`` is a Max() over the conversation, so its range filter
    lands in HAVING rather than WHERE."""

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-conversations")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)
        _make_interaction(
            session_id="s1",
            created=django_timezone.make_aware(datetime.datetime(2026, 7, 10, 9, 0)),
        )
        _make_interaction(
            session_id="s1",
            created=django_timezone.make_aware(datetime.datetime(2026, 7, 20, 9, 0)),
        )

    def test_boundary_day_is_inclusive(self):
        # The conversation last moved on the 20th; asking for activity up to
        # and including that day must keep it.
        response = self.client.get(self.url, {"last_active_before": "2026-07-20"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_window_closing_before_last_activity_excludes_it(self):
        response = self.client.get(self.url, {"last_active_before": "2026-07-19"})

        self.assertEqual(response.data, [])

    def test_window_opening_after_last_activity_excludes_it(self):
        response = self.client.get(self.url, {"last_active_after": "2026-07-21"})

        self.assertEqual(response.data, [])


class KpiHonoursConversationFiltersTest(test.APITestCase):
    """The summary widget is handed the table's whole filter object, so a
    conversation-level bound it silently ignored would leave the headline
    numbers contradicting the rows underneath them."""

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-kpi")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)
        _make_interaction(session_id="cheap", input_tokens=10, output_tokens=0)
        _make_interaction(session_id="pricey", input_tokens=900, output_tokens=100)

    def test_token_bound_narrows_the_rollup(self):
        response = self.client.get(self.url, {"total_tokens_min": 500})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["interactions_total"], 1)
        self.assertEqual(response.data["input_tokens_total"], 900)

    def test_unfiltered_rollup_still_counts_everything(self):
        response = self.client.get(self.url)

        self.assertEqual(response.data["interactions_total"], 2)
        self.assertEqual(response.data["input_tokens_total"], 910)


class KpiReviewedCountsThreadsTest(test.APITestCase):
    """Review is per thread, so the counter must not report turns."""

    def setUp(self):
        self.url = reverse("anonymous-chat-interaction-kpi")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)

    def test_two_reviewed_turns_in_one_thread_count_once(self):
        for index in range(2):
            interaction = _make_interaction(session_id="s1")
            anonymous_models.AnonymousChatFeedback.objects.create(
                interaction=interaction,
                score=1,
                llm_reviewed_at=django_timezone.now(),
            )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reviewed_total"], 1)
