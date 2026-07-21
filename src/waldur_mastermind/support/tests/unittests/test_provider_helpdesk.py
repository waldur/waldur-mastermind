from datetime import timedelta
from unittest.mock import patch

from constance.test.unittest import override_config
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_mastermind.support import models
from waldur_mastermind.support.backend import SupportBackendError
from waldur_mastermind.support.backend.basic import BasicBackend
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend
from waldur_mastermind.support.tests import factories


class TestIssueStatusTransitionModel(TestCase):
    """Tests for IssueStatusTransition.is_transition_allowed() class method."""

    def test_is_transition_allowed_returns_true_when_no_transitions_defined(self):
        """Backward compatibility: all transitions allowed when table is empty."""
        self.assertTrue(
            models.IssueStatusTransition.is_transition_allowed("Open", "Closed")
        )

    def test_is_transition_allowed_returns_true_for_defined_transition(self):
        factories.IssueStatusTransitionFactory(
            from_status="Open", to_status="In Progress"
        )
        self.assertTrue(
            models.IssueStatusTransition.is_transition_allowed("Open", "In Progress")
        )

    def test_is_transition_allowed_returns_false_for_undefined_transition(self):
        factories.IssueStatusTransitionFactory(
            from_status="Open", to_status="In Progress"
        )
        self.assertFalse(
            models.IssueStatusTransition.is_transition_allowed("Open", "Closed")
        )

    def test_is_transition_allowed_returns_false_for_reversed_transition(self):
        """A -> B does not imply B -> A."""
        factories.IssueStatusTransitionFactory(
            from_status="Open", to_status="In Progress"
        )
        self.assertFalse(
            models.IssueStatusTransition.is_transition_allowed("In Progress", "Open")
        )

    def test_unique_together_constraint(self):
        factories.IssueStatusTransitionFactory(from_status="Open", to_status="Closed")
        with self.assertRaises(IntegrityError):
            factories.IssueStatusTransitionFactory(
                from_status="Open", to_status="Closed"
            )


class TestProviderSupportUserOpenTicketCount(TestCase):
    """Tests for ProviderSupportUser.open_ticket_count and has_capacity properties."""

    def setUp(self):
        self.provider_user = factories.ProviderSupportUserFactory(max_open_tickets=3)

    def test_open_ticket_count_returns_zero_when_no_issues_assigned(self):
        self.assertEqual(self.provider_user.open_ticket_count, 0)

    def test_open_ticket_count_counts_unresolved_issues(self):
        factories.IssueFactory(
            provider_assignee=self.provider_user, resolution_date=None
        )
        factories.IssueFactory(
            provider_assignee=self.provider_user, resolution_date=None
        )
        self.assertEqual(self.provider_user.open_ticket_count, 2)

    def test_open_ticket_count_excludes_resolved_issues(self):
        factories.IssueFactory(
            provider_assignee=self.provider_user, resolution_date=None
        )
        factories.IssueFactory(
            provider_assignee=self.provider_user, resolution_date=timezone.now()
        )
        self.assertEqual(self.provider_user.open_ticket_count, 1)

    def test_has_capacity_returns_true_when_under_limit(self):
        factories.IssueFactory(
            provider_assignee=self.provider_user, resolution_date=None
        )
        self.assertTrue(self.provider_user.has_capacity)

    def test_has_capacity_returns_false_when_at_limit(self):
        for _ in range(3):
            factories.IssueFactory(
                provider_assignee=self.provider_user, resolution_date=None
            )
        self.assertFalse(self.provider_user.has_capacity)

    def test_has_capacity_returns_false_when_over_limit(self):
        for _ in range(4):
            factories.IssueFactory(
                provider_assignee=self.provider_user, resolution_date=None
            )
        self.assertFalse(self.provider_user.has_capacity)


class TestProviderCannedResponseRender(TestCase):
    """Tests for ProviderCannedResponse.render() method."""

    def test_render_with_context_variables(self):
        response = factories.ProviderCannedResponseFactory(
            text="Hello {{ customer_name }}, ticket {{ ticket_id }} is in progress."
        )
        result = response.render({"customer_name": "Acme Corp", "ticket_id": "WLD-001"})
        self.assertEqual(result, "Hello Acme Corp, ticket WLD-001 is in progress.")

    def test_render_with_empty_context(self):
        response = factories.ProviderCannedResponseFactory(
            text="Thank you for contacting support."
        )
        result = response.render()
        self.assertEqual(result, "Thank you for contacting support.")

    def test_render_with_none_context(self):
        response = factories.ProviderCannedResponseFactory(text="Static response text.")
        result = response.render(None)
        self.assertEqual(result, "Static response text.")

    def test_render_with_missing_variable_produces_empty_string(self):
        response = factories.ProviderCannedResponseFactory(
            text="Hello {{ customer_name }}!"
        )
        result = response.render({})
        self.assertEqual(result, "Hello !")


class TestCannedResponseRender(TestCase):
    """Tests for CannedResponse.render() method."""

    def test_render_with_context_variables(self):
        response = factories.CannedResponseFactory(
            text="Dear {{ user_name }}, your issue {{ issue_key }} is resolved."
        )
        result = response.render({"user_name": "Alice", "issue_key": "WLD-123"})
        self.assertEqual(result, "Dear Alice, your issue WLD-123 is resolved.")

    def test_render_with_empty_context(self):
        response = factories.CannedResponseFactory(text="Thank you for your patience.")
        result = response.render()
        self.assertEqual(result, "Thank you for your patience.")

    def test_render_with_none_context(self):
        response = factories.CannedResponseFactory(text="Fixed text.")
        result = response.render(None)
        self.assertEqual(result, "Fixed text.")

    def test_render_with_django_template_filter(self):
        response = factories.CannedResponseFactory(text="Hello {{ name|upper }}!")
        result = response.render({"name": "alice"})
        self.assertEqual(result, "Hello ALICE!")


@override_config(
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)
class TestBasicBackendCreateIssue(TestCase):
    """Tests for BasicBackend.create_issue() method."""

    def setUp(self):
        self.backend = BasicBackend()
        models.IssueStatus.objects.create(
            name="Open", type=models.IssueStatus.Types.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=models.IssueStatus.Types.CANCELED
        )

    def _create_unsaved_issue(self, **kwargs):
        """Create a saved Issue with blank backend fields, ready for create_issue()."""
        defaults = {"backend_id": "", "key": "", "status": "", "backend_name": "basic"}
        defaults.update(kwargs)
        issue = factories.IssueFactory(**defaults)
        # Reset fields that IssueFactory auto-populates so BasicBackend can set them
        models.Issue.objects.filter(pk=issue.pk).update(
            backend_id=defaults["backend_id"],
            key=defaults["key"],
            status=defaults["status"],
        )
        issue.refresh_from_db()
        return issue

    def test_create_issue_sets_backend_id_with_wld_prefix(self):
        issue = self._create_unsaved_issue()
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        self.assertTrue(issue.backend_id.startswith("WLD-"))

    def test_create_issue_sets_key_equal_to_backend_id(self):
        issue = self._create_unsaved_issue()
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.key, issue.backend_id)

    def test_create_issue_sets_default_status_when_empty(self):
        issue = self._create_unsaved_issue()
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "Open")

    def test_create_issue_preserves_existing_status(self):
        issue = self._create_unsaved_issue(status="In Review")
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "In Review")

    @override_config(WALDUR_SUPPORT_SLA_ENABLED=True)
    def test_create_issue_sets_sla_deadlines(self):
        issue = self._create_unsaved_issue()
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        self.assertIsNotNone(issue.first_response_deadline)
        self.assertIsNotNone(issue.resolution_deadline)

    def test_create_issue_skips_sla_deadlines_when_disabled(self):
        # SLA tracking is off by default; the basic path must not set deadlines.
        issue = self._create_unsaved_issue()
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        self.assertIsNone(issue.first_response_deadline)
        self.assertIsNone(issue.resolution_deadline)

    @override_config(
        WALDUR_SUPPORT_SLA_ENABLED=True,
        WALDUR_SUPPORT_SLA_RESPONSE_HOURS=8,
        WALDUR_SUPPORT_SLA_RESOLUTION_HOURS=48,
    )
    @freeze_time("2025-01-15 12:00:00")
    def test_create_issue_sla_deadlines_use_config_values(self):
        issue = self._create_unsaved_issue()
        self.backend.create_issue(issue)
        issue.refresh_from_db()
        now = timezone.now()
        self.assertEqual(issue.first_response_deadline, now + timedelta(hours=8))
        self.assertEqual(issue.resolution_deadline, now + timedelta(hours=48))


@override_config(
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)
class TestBasicBackendCreateComment(TestCase):
    """Tests for BasicBackend.create_comment() and first_response_at tracking."""

    def setUp(self):
        self.backend = BasicBackend()
        models.IssueStatus.objects.create(
            name="Open", type=models.IssueStatus.Types.RESOLVED
        )

    def test_create_comment_sets_first_response_at_for_non_caller_author(self):
        issue = factories.IssueFactory(backend_name="basic")
        staff_support_user = factories.SupportUserFactory()
        comment = factories.CommentFactory(
            issue=issue,
            author=staff_support_user,
            backend_id="",
            backend_name="basic",
        )
        self.backend.create_comment(comment)
        issue.refresh_from_db()
        self.assertIsNotNone(issue.first_response_at)

    def test_create_comment_does_not_set_first_response_at_for_caller(self):
        issue = factories.IssueFactory(backend_name="basic")
        caller_support_user = factories.SupportUserFactory(user=issue.caller)
        comment = factories.CommentFactory(
            issue=issue,
            author=caller_support_user,
            backend_id="",
            backend_name="basic",
        )
        self.backend.create_comment(comment)
        issue.refresh_from_db()
        self.assertIsNone(issue.first_response_at)

    def test_create_comment_does_not_overwrite_existing_first_response_at(self):
        original_time = timezone.now() - timedelta(hours=1)
        issue = factories.IssueFactory(
            backend_name="basic", first_response_at=original_time
        )
        staff_support_user = factories.SupportUserFactory()
        comment = factories.CommentFactory(
            issue=issue,
            author=staff_support_user,
            backend_id="",
            backend_name="basic",
        )
        self.backend.create_comment(comment)
        issue.refresh_from_db()
        self.assertEqual(issue.first_response_at, original_time)

    def test_create_comment_sets_backend_id_with_wld_c_prefix(self):
        issue = factories.IssueFactory(backend_name="basic")
        comment = factories.CommentFactory(
            issue=issue, backend_id="", backend_name="basic"
        )
        self.backend.create_comment(comment)
        comment.refresh_from_db()
        self.assertTrue(comment.backend_id.startswith("WLD-C-"))


@override_config(
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)
class TestBasicBackendUpdateIssue(TestCase):
    """Tests for BasicBackend.update_issue() with status transition validation."""

    def setUp(self):
        self.backend = BasicBackend()
        models.IssueStatus.objects.create(
            name="Open", type=models.IssueStatus.Types.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=models.IssueStatus.Types.CANCELED
        )

    def test_update_issue_allows_transition_when_no_transitions_defined(self):
        issue = factories.IssueFactory(status="Open", backend_name="basic")
        issue.status = "In Progress"
        self.backend.update_issue(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "In Progress")

    def test_update_issue_allows_valid_transition_when_transitions_defined(self):
        factories.IssueStatusTransitionFactory(
            from_status="Open", to_status="In Progress"
        )
        issue = factories.IssueFactory(status="Open", backend_name="basic")
        issue.status = "In Progress"
        self.backend.update_issue(issue)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "In Progress")

    def test_update_issue_blocks_invalid_transition_when_transitions_defined(self):
        factories.IssueStatusTransitionFactory(
            from_status="Open", to_status="In Progress"
        )
        issue = factories.IssueFactory(status="Open", backend_name="basic")
        issue.status = "Closed"
        with self.assertRaises(SupportBackendError):
            self.backend.update_issue(issue)

    def test_update_issue_sets_resolution_date_when_resolved(self):
        issue = factories.IssueFactory(status="In Progress", backend_name="basic")
        issue.status = "Open"  # "Open" has type RESOLVED in our setUp
        self.backend.update_issue(issue)
        issue.refresh_from_db()
        self.assertIsNotNone(issue.resolution_date)

    def test_update_issue_sets_resolution_date_when_canceled(self):
        issue = factories.IssueFactory(status="In Progress", backend_name="basic")
        issue.status = "Canceled"
        self.backend.update_issue(issue)
        issue.refresh_from_db()
        self.assertIsNotNone(issue.resolution_date)

    def test_update_issue_without_status_change_does_not_set_resolution_date(self):
        issue = factories.IssueFactory(status="Open", backend_name="basic")
        issue.summary = "Updated summary"
        self.backend.update_issue(issue)
        issue.refresh_from_db()
        self.assertIsNone(issue.resolution_date)


class TestBasicBackendFromSettings(TestCase):
    """Tests for BasicBackend.from_settings() class method."""

    def test_from_settings_returns_basic_backend_instance(self):
        backend = BasicBackend.from_settings()
        self.assertIsInstance(backend, BasicBackend)

    def test_from_settings_with_dict_returns_basic_backend_instance(self):
        backend = BasicBackend.from_settings({"some_key": "some_value"})
        self.assertIsInstance(backend, BasicBackend)

    def test_from_settings_with_none_returns_basic_backend_instance(self):
        backend = BasicBackend.from_settings(None)
        self.assertIsInstance(backend, BasicBackend)


class TestBackendGetConfigFallback(TestCase):
    """Tests for backend _get_config method (using ZammadServiceBackend)."""

    @patch("waldur_mastermind.support.backend.zammad.ZammadBackend")
    def test_get_config_returns_override_value_when_present(self, mock_backend):
        backend = ZammadServiceBackend(
            settings_override={"ZAMMAD_COMMENT_COOLDOWN_DURATION": 99}
        )
        self.assertEqual(backend._get_config("ZAMMAD_COMMENT_COOLDOWN_DURATION"), 99)

    @override_config(ZAMMAD_COMMENT_COOLDOWN_DURATION=5)
    @patch("waldur_mastermind.support.backend.zammad.ZammadBackend")
    def test_get_config_falls_back_to_constance_when_key_not_in_override(
        self, mock_backend
    ):
        backend = ZammadServiceBackend(settings_override={})
        self.assertEqual(backend._get_config("ZAMMAD_COMMENT_COOLDOWN_DURATION"), 5)

    @patch("waldur_mastermind.support.backend.zammad.ZammadBackend")
    def test_get_config_returns_default_when_key_not_in_override_or_constance(
        self, mock_backend
    ):
        backend = ZammadServiceBackend(settings_override={})
        result = backend._get_config("NONEXISTENT_KEY", default="fallback")
        self.assertEqual(result, "fallback")

    @patch("waldur_mastermind.support.backend.zammad.ZammadBackend")
    def test_get_config_override_takes_precedence_over_constance(self, mock_backend):
        backend = ZammadServiceBackend(
            settings_override={"ZAMMAD_COMMENT_COOLDOWN_DURATION": 42}
        )
        # Even if Constance has a different value, override wins
        self.assertEqual(backend._get_config("ZAMMAD_COMMENT_COOLDOWN_DURATION"), 42)
