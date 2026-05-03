"""Tests for the staff/support read ViewSets exposing anonymous
chat transcripts and feedback rows.

Risk profile (per the backend plan): a permission-gating slip silently
leaks every anonymous transcript to every authenticated user. So the
gating tests are explicit and duplicated across two layers
(``permission_classes`` and ``get_queryset``).
"""

import inspect

from django.test import SimpleTestCase
from django.urls import reverse
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

        self.s1_a = _make_interaction(
            session_id="session-A",
            user_slug="slug-X",
        )
        self.s1_b = _make_interaction(
            session_id="session-A",
            user_slug="slug-X",
        )
        self.s2 = _make_interaction(
            session_id="session-B",
            user_slug="slug-X",
            is_flagged=True,
            severity="high",
        )
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

    def test_filter_has_negative_feedback(self):
        response = self.client.get(self.list_url, {"has_negative_feedback": "true"})
        rows = self._rows(response)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "session-B")

    def test_by_session_returns_ordered_turns(self):
        response = self.client.get(self.session_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # Ordered chronologically — s1_a before s1_b
        self.assertEqual(response.data[0]["uuid"], str(self.s1_a.uuid))

    def test_by_user_groups_across_sessions(self):
        response = self.client.get(self.user_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # slug-X owns session-A (2 interactions) + session-B (1) = 3
        self.assertEqual(len(response.data), 3)

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
