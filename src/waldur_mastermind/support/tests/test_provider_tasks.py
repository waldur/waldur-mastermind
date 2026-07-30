from datetime import timedelta
from unittest import mock

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models, tasks
from waldur_mastermind.support.tests import factories


class RouteIssueToProviderTest(TestCase):
    """Tests for the route_issue_to_provider task."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.customer
        )
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.resource = marketplace_factories.ResourceFactory(offering=self.offering)
        self.helpdesk = factories.ProviderHelpdeskFactory(
            service_provider=self.service_provider
        )
        ct = ContentType.objects.get_for_model(self.resource)
        self.issue = factories.IssueFactory(
            resource_content_type=ct,
            resource_object_id=self.resource.id,
            backend_id="WLD-100",
            customer=self.customer,
        )

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_creates_child_issue_when_provider_helpdesk_exists(self, mock_get_backend):
        mock_backend = mock.Mock()
        mock_get_backend.return_value = mock_backend

        tasks.route_issue_to_provider(self.issue.id)

        child_issue = models.Issue.objects.filter(parent_issue=self.issue).first()
        self.assertIsNotNone(child_issue)
        self.assertEqual(child_issue.parent_issue, self.issue)
        self.assertEqual(child_issue.summary, self.issue.summary)
        mock_backend.create_issue.assert_called_once_with(child_issue)

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_sets_provider_helpdesk_on_child_issue(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()

        tasks.route_issue_to_provider(self.issue.id)

        child_issue = models.Issue.objects.filter(parent_issue=self.issue).first()
        self.assertIsNotNone(child_issue)
        self.assertEqual(child_issue.provider_helpdesk, self.helpdesk)

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_appends_to_parent_processing_log(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()

        tasks.route_issue_to_provider(self.issue.id)

        self.issue.refresh_from_db()
        self.assertGreater(len(self.issue.processing_log), 0)
        last_entry = self.issue.processing_log[-1]
        self.assertEqual(last_entry["event"], "routed_to_provider")
        self.assertIn("details", last_entry)
        self.assertIn("child_issue_uuid", last_entry["details"])

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_increments_failed_routing_count_on_failure(self, mock_get_backend):
        mock_backend = mock.Mock()
        mock_backend.create_issue.side_effect = Exception("Backend error")
        mock_get_backend.return_value = mock_backend

        initial_count = self.helpdesk.failed_routing_count

        with self.assertRaises(Exception):
            tasks.route_issue_to_provider(self.issue.id)

        self.helpdesk.refresh_from_db()
        self.assertEqual(self.helpdesk.failed_routing_count, initial_count + 1)

    def test_skips_routing_when_issue_does_not_exist(self):
        # Should not raise, just log and return
        tasks.route_issue_to_provider(999999)

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_skips_routing_when_already_routed(self, mock_get_backend):
        mock_get_backend.return_value = mock.Mock()

        # Create a child issue to simulate already-routed
        factories.IssueFactory(parent_issue=self.issue)

        tasks.route_issue_to_provider(self.issue.id)

        mock_get_backend.assert_not_called()

    def test_skips_routing_when_issue_has_no_resource(self):
        issue = factories.IssueFactory(
            resource_content_type=None,
            resource_object_id=None,
            backend_id="WLD-200",
        )
        # Should not raise
        tasks.route_issue_to_provider(issue.id)
        self.assertFalse(models.Issue.objects.filter(parent_issue=issue).exists())

    def test_skips_routing_when_no_service_provider(self):
        other_customer = structure_factories.CustomerFactory()
        other_offering = marketplace_factories.OfferingFactory(customer=other_customer)
        other_resource = marketplace_factories.ResourceFactory(offering=other_offering)
        ct = ContentType.objects.get_for_model(other_resource)
        issue = factories.IssueFactory(
            resource_content_type=ct,
            resource_object_id=other_resource.id,
            backend_id="WLD-300",
        )
        tasks.route_issue_to_provider(issue.id)
        self.assertFalse(models.Issue.objects.filter(parent_issue=issue).exists())

    def test_skips_routing_when_no_active_helpdesk(self):
        self.helpdesk.is_active = False
        self.helpdesk.save()

        tasks.route_issue_to_provider(self.issue.id)
        self.assertFalse(models.Issue.objects.filter(parent_issue=self.issue).exists())

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_routes_by_offering_when_no_resource(self, mock_get_backend):
        # A ticket opened about an offering (no resource) routes to that
        # offering's provider helpdesk.
        mock_get_backend.return_value = mock.Mock()
        issue = factories.IssueFactory(
            offering=self.offering,
            backend_id="WLD-400",
            customer=self.customer,
        )

        tasks.route_issue_to_provider(issue.id)

        child_issue = models.Issue.objects.filter(parent_issue=issue).first()
        self.assertIsNotNone(child_issue)
        self.assertEqual(child_issue.provider_helpdesk, self.helpdesk)

    @mock.patch("waldur_mastermind.support.backend.get_backend_for_provider")
    def test_offering_on_issue_takes_precedence_over_resource(self, mock_get_backend):
        # When both are set, the explicit issue.offering wins over the offering
        # behind the attached resource.
        mock_get_backend.return_value = mock.Mock()
        other_customer = structure_factories.CustomerFactory()
        other_provider = marketplace_factories.ServiceProviderFactory(
            customer=other_customer
        )
        other_offering = marketplace_factories.OfferingFactory(customer=other_customer)
        other_helpdesk = factories.ProviderHelpdeskFactory(
            service_provider=other_provider
        )
        ct = ContentType.objects.get_for_model(self.resource)
        issue = factories.IssueFactory(
            resource_content_type=ct,
            resource_object_id=self.resource.id,
            offering=other_offering,
            backend_id="WLD-500",
            customer=self.customer,
        )

        tasks.route_issue_to_provider(issue.id)

        child_issue = models.Issue.objects.filter(parent_issue=issue).first()
        self.assertIsNotNone(child_issue)
        self.assertEqual(child_issue.provider_helpdesk, other_helpdesk)

    def test_skips_routing_when_no_resource_and_no_offering(self):
        issue = factories.IssueFactory(
            resource_content_type=None,
            resource_object_id=None,
            offering=None,
            backend_id="WLD-600",
        )
        tasks.route_issue_to_provider(issue.id)
        self.assertFalse(models.Issue.objects.filter(parent_issue=issue).exists())


class ForwardCommentToChildTest(TestCase):
    """Tests for the forward_comment_to_child task."""

    def setUp(self):
        self.parent_issue = factories.IssueFactory(backend_id="WLD-P1")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue, backend_id="WLD-C1"
        )
        self.author = factories.SupportUserFactory()

    def test_creates_forwarded_comment_on_child_issue(self):
        comment = factories.CommentFactory(
            issue=self.parent_issue,
            author=self.author,
            description="Parent comment text",
            is_public=True,
        )

        tasks.forward_comment_to_child(comment.id)

        child_comments = models.Comment.objects.filter(issue=self.child_issue)
        self.assertEqual(child_comments.count(), 1)
        forwarded = child_comments.first()
        self.assertEqual(forwarded.description, "Parent comment text")
        self.assertEqual(forwarded.author, self.author)

    def test_forwarded_comment_has_is_forwarded_true(self):
        comment = factories.CommentFactory(
            issue=self.parent_issue,
            author=self.author,
            description="Forwarded text",
            is_public=True,
        )

        tasks.forward_comment_to_child(comment.id)

        forwarded = models.Comment.objects.filter(issue=self.child_issue).first()
        self.assertIsNotNone(forwarded)
        self.assertTrue(forwarded.is_forwarded)

    def test_forwards_to_multiple_child_issues(self):
        child_issue_2 = factories.IssueFactory(
            parent_issue=self.parent_issue, backend_id="WLD-C2"
        )
        comment = factories.CommentFactory(
            issue=self.parent_issue,
            author=self.author,
            description="Broadcast comment",
            is_public=True,
        )

        tasks.forward_comment_to_child(comment.id)

        self.assertEqual(
            models.Comment.objects.filter(issue=self.child_issue).count(), 1
        )
        self.assertEqual(models.Comment.objects.filter(issue=child_issue_2).count(), 1)

    def test_does_not_fail_when_comment_does_not_exist(self):
        # Should log error but not raise
        tasks.forward_comment_to_child(999999)

    def test_preserves_is_public_flag(self):
        comment = factories.CommentFactory(
            issue=self.parent_issue,
            author=self.author,
            description="Public comment",
            is_public=True,
        )

        tasks.forward_comment_to_child(comment.id)

        forwarded = models.Comment.objects.filter(issue=self.child_issue).first()
        self.assertTrue(forwarded.is_public)


class PropagateCommentToParentTest(TestCase):
    """Tests for the propagate_comment_to_parent task."""

    def setUp(self):
        self.parent_issue = factories.IssueFactory(backend_id="WLD-P1")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue, backend_id="WLD-C1"
        )
        self.author = factories.SupportUserFactory()

    def test_creates_forwarded_comment_on_parent_issue(self):
        comment = factories.CommentFactory(
            issue=self.child_issue,
            author=self.author,
            description="Provider response",
            is_public=True,
        )

        tasks.propagate_comment_to_parent(comment.id)

        parent_comments = models.Comment.objects.filter(
            issue=self.parent_issue, is_forwarded=True
        )
        self.assertEqual(parent_comments.count(), 1)
        propagated = parent_comments.first()
        self.assertEqual(propagated.description, "Provider response")
        self.assertEqual(propagated.author, self.author)

    def test_propagated_comment_has_is_forwarded_true(self):
        comment = factories.CommentFactory(
            issue=self.child_issue,
            author=self.author,
            description="Propagated text",
            is_public=True,
        )

        tasks.propagate_comment_to_parent(comment.id)

        propagated = models.Comment.objects.filter(
            issue=self.parent_issue, is_forwarded=True
        ).first()
        self.assertIsNotNone(propagated)
        self.assertTrue(propagated.is_forwarded)

    def test_skips_when_child_has_no_parent(self):
        orphan_issue = factories.IssueFactory(
            parent_issue=None, backend_id="WLD-ORPHAN"
        )
        comment = factories.CommentFactory(
            issue=orphan_issue,
            author=self.author,
            description="Orphan comment",
            is_public=True,
        )

        tasks.propagate_comment_to_parent(comment.id)

        # No new comments should be created on any issue except the original
        self.assertFalse(models.Comment.objects.filter(is_forwarded=True).exists())

    def test_does_not_fail_when_comment_does_not_exist(self):
        # Should log error but not raise
        tasks.propagate_comment_to_parent(999999)


class CheckSlaBreachesTest(TestCase):
    """Tests for the check_sla_breaches task."""

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_marks_issue_breached_when_first_response_deadline_passed(self):
        issue = factories.IssueFactory(
            first_response_deadline=timezone.now() - timedelta(hours=1),
            first_response_at=None,
            sla_breached=False,
            resolution_date=None,
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertTrue(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_marks_issue_breached_when_resolution_deadline_passed(self):
        issue = factories.IssueFactory(
            resolution_deadline=timezone.now() - timedelta(hours=1),
            sla_breached=False,
            resolution_date=None,
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertTrue(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_does_not_mark_issue_when_deadlines_not_yet_passed(self):
        issue = factories.IssueFactory(
            first_response_deadline=timezone.now() + timedelta(hours=1),
            resolution_deadline=timezone.now() + timedelta(days=1),
            first_response_at=None,
            sla_breached=False,
            resolution_date=None,
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertFalse(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_does_not_mark_issue_when_first_response_provided(self):
        issue = factories.IssueFactory(
            first_response_deadline=timezone.now() - timedelta(hours=1),
            first_response_at=timezone.now() - timedelta(hours=2),
            sla_breached=False,
            resolution_date=None,
            resolution_deadline=None,
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertFalse(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_does_not_mark_resolved_issue(self):
        issue = factories.IssueFactory(
            resolution_deadline=timezone.now() - timedelta(hours=1),
            sla_breached=False,
            resolution_date=timezone.now() - timedelta(minutes=30),
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertFalse(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_does_not_re_mark_already_breached_issue(self):
        issue = factories.IssueFactory(
            resolution_deadline=timezone.now() - timedelta(hours=1),
            sla_breached=True,
            resolution_date=None,
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertTrue(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=False)
    def test_does_nothing_when_support_disabled(self):
        issue = factories.IssueFactory(
            first_response_deadline=timezone.now() - timedelta(hours=1),
            first_response_at=None,
            sla_breached=False,
            resolution_date=None,
        )

        tasks.check_sla_breaches()

        issue.refresh_from_db()
        self.assertFalse(issue.sla_breached)

    @override_config(WALDUR_SUPPORT_ENABLED=True)
    def test_handles_multiple_breached_issues(self):
        issue1 = factories.IssueFactory(
            first_response_deadline=timezone.now() - timedelta(hours=1),
            first_response_at=None,
            sla_breached=False,
            resolution_date=None,
        )
        issue2 = factories.IssueFactory(
            resolution_deadline=timezone.now() - timedelta(hours=2),
            sla_breached=False,
            resolution_date=None,
        )

        tasks.check_sla_breaches()

        issue1.refresh_from_db()
        issue2.refresh_from_db()
        self.assertTrue(issue1.sla_breached)
        self.assertTrue(issue2.sla_breached)


class DispatchRoutingOnIssueCreateTest(TestCase):
    """Tests for the dispatch_routing_on_issue_create signal handler.

    Uses TestCase with captureOnCommitCallbacks to test on_commit callbacks.
    """

    @mock.patch("waldur_mastermind.support.tasks.route_issue_to_provider.delay")
    @override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=True)
    def test_dispatches_task_when_routing_enabled_and_issue_has_backend_id(
        self, mock_route
    ):
        resource = marketplace_factories.ResourceFactory()
        ct = ContentType.objects.get_for_model(resource)
        with self.captureOnCommitCallbacks(execute=True):
            issue = factories.IssueFactory(
                backend_id="WLD-123",
                resource_content_type=ct,
                resource_object_id=resource.id,
            )

        mock_route.assert_called_once_with(issue.id)

    @mock.patch("waldur_mastermind.support.tasks.route_issue_to_provider.delay")
    @override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=False)
    def test_does_not_dispatch_when_routing_disabled(self, mock_route):
        with self.captureOnCommitCallbacks(execute=True):
            factories.IssueFactory(backend_id="WLD-124")

        mock_route.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.route_issue_to_provider.delay")
    @override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=True)
    def test_does_not_dispatch_for_child_issues(self, mock_route):
        with self.captureOnCommitCallbacks(execute=True):
            parent = factories.IssueFactory(backend_id="WLD-PARENT")

        mock_route.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            factories.IssueFactory(
                parent_issue=parent,
                backend_id="WLD-CHILD",
            )

        mock_route.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.route_issue_to_provider.delay")
    @override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=True)
    def test_does_not_dispatch_when_no_backend_id(self, mock_route):
        with self.captureOnCommitCallbacks(execute=True):
            factories.IssueFactory(backend_id="")

        mock_route.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.route_issue_to_provider.delay")
    @override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=True)
    def test_does_not_dispatch_on_issue_update(self, mock_route):
        with self.captureOnCommitCallbacks(execute=True):
            issue = factories.IssueFactory(backend_id="WLD-125")

        mock_route.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            issue.summary = "Updated summary"
            issue.save()

        mock_route.assert_not_called()


class ForwardCommentToChildrenHandlerTest(TestCase):
    """Tests for the forward_comment_to_children signal handler.

    Uses TestCase with captureOnCommitCallbacks to test on_commit callbacks.
    """

    def setUp(self):
        self.parent_issue = factories.IssueFactory(backend_id="WLD-P1")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue, backend_id="WLD-C1"
        )
        self.author = factories.SupportUserFactory()

    @mock.patch("waldur_mastermind.support.tasks.forward_comment_to_child.delay")
    def test_dispatches_task_for_public_non_forwarded_comment_on_parent(
        self, mock_forward
    ):
        with self.captureOnCommitCallbacks(execute=True):
            comment = models.Comment.objects.create(
                issue=self.parent_issue,
                author=self.author,
                description="New public comment",
                is_public=True,
                is_forwarded=False,
            )

        mock_forward.assert_called_once_with(comment.id)

    @mock.patch("waldur_mastermind.support.tasks.forward_comment_to_child.delay")
    def test_does_not_dispatch_for_forwarded_comments(self, mock_forward):
        with self.captureOnCommitCallbacks(execute=True):
            models.Comment.objects.create(
                issue=self.parent_issue,
                author=self.author,
                description="Forwarded comment",
                is_public=True,
                is_forwarded=True,
            )

        mock_forward.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.forward_comment_to_child.delay")
    def test_does_not_dispatch_for_private_comments(self, mock_forward):
        with self.captureOnCommitCallbacks(execute=True):
            models.Comment.objects.create(
                issue=self.parent_issue,
                author=self.author,
                description="Private comment",
                is_public=False,
                is_forwarded=False,
            )

        mock_forward.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.forward_comment_to_child.delay")
    def test_does_not_dispatch_for_issue_without_children(self, mock_forward):
        standalone_issue = factories.IssueFactory(backend_id="WLD-SOLO")

        with self.captureOnCommitCallbacks(execute=True):
            models.Comment.objects.create(
                issue=standalone_issue,
                author=self.author,
                description="Comment on standalone issue",
                is_public=True,
                is_forwarded=False,
            )

        mock_forward.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.forward_comment_to_child.delay")
    def test_does_not_dispatch_on_comment_update(self, mock_forward):
        with self.captureOnCommitCallbacks(execute=True):
            comment = models.Comment.objects.create(
                issue=self.parent_issue,
                author=self.author,
                description="Original",
                is_public=True,
                is_forwarded=False,
            )

        mock_forward.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            comment.description = "Updated"
            comment.save()

        mock_forward.assert_not_called()


class PropagateCommentToParentHandlerTest(TestCase):
    """Tests for the propagate_comment_to_parent signal handler.

    Uses TestCase with captureOnCommitCallbacks to test on_commit callbacks.
    """

    def setUp(self):
        self.parent_issue = factories.IssueFactory(backend_id="WLD-P1")
        self.child_issue = factories.IssueFactory(
            parent_issue=self.parent_issue, backend_id="WLD-C1"
        )
        self.author = factories.SupportUserFactory()

    @mock.patch("waldur_mastermind.support.tasks.propagate_comment_to_parent.delay")
    def test_dispatches_task_for_public_non_forwarded_comment_on_child(
        self, mock_propagate
    ):
        with self.captureOnCommitCallbacks(execute=True):
            comment = models.Comment.objects.create(
                issue=self.child_issue,
                author=self.author,
                description="Provider reply",
                is_public=True,
                is_forwarded=False,
            )

        mock_propagate.assert_called_once_with(comment.id)

    @mock.patch("waldur_mastermind.support.tasks.propagate_comment_to_parent.delay")
    def test_does_not_dispatch_for_forwarded_comments(self, mock_propagate):
        with self.captureOnCommitCallbacks(execute=True):
            models.Comment.objects.create(
                issue=self.child_issue,
                author=self.author,
                description="Already forwarded",
                is_public=True,
                is_forwarded=True,
            )

        mock_propagate.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.propagate_comment_to_parent.delay")
    def test_does_not_dispatch_for_private_comments(self, mock_propagate):
        with self.captureOnCommitCallbacks(execute=True):
            models.Comment.objects.create(
                issue=self.child_issue,
                author=self.author,
                description="Private note",
                is_public=False,
                is_forwarded=False,
            )

        mock_propagate.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.propagate_comment_to_parent.delay")
    def test_does_not_dispatch_for_issue_without_parent(self, mock_propagate):
        standalone = factories.IssueFactory(parent_issue=None, backend_id="WLD-ALONE")

        with self.captureOnCommitCallbacks(execute=True):
            models.Comment.objects.create(
                issue=standalone,
                author=self.author,
                description="Root issue comment",
                is_public=True,
                is_forwarded=False,
            )

        mock_propagate.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.propagate_comment_to_parent.delay")
    def test_does_not_dispatch_on_comment_update(self, mock_propagate):
        with self.captureOnCommitCallbacks(execute=True):
            comment = models.Comment.objects.create(
                issue=self.child_issue,
                author=self.author,
                description="Original reply",
                is_public=True,
                is_forwarded=False,
            )

        mock_propagate.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            comment.description = "Edited reply"
            comment.save()

        mock_propagate.assert_not_called()
