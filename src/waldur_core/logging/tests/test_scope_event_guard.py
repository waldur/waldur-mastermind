from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.test import TestCase

from waldur_core.logging import utils
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.users.models import Invitation
from waldur_core.users.tests import factories as users_factories


def only_staff(user, scope):
    return user.is_staff


def own_actions(user, scope):
    return None if user.is_staff else Q(context__user_uuid=user.uuid.hex)


class ScopeEventGuardWiderFeedsTest(TestCase):
    """A guarded or row-filtered scope's events stay off wider feeds unless
    its guard or filter says those it narrows the feed for cannot read them."""

    def setUp(self):
        for registry in (utils._scope_event_guards, utils._scope_event_filters):
            patcher = mock.patch.dict(registry, clear=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_guards_on_wider_feeds", "_filters_on_wider_feeds"):
            patcher = mock.patch.object(utils, name, set())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project

    def grant_role(self):
        user = structure_factories.UserFactory()
        self.project.add_user(user, ProjectRole.MEMBER)
        return Feed.objects.filter(
            event__event_type=EventType.ROLE_GRANTED,
            event__context__affected_user_uuid=user.uuid.hex,
        )

    def filed_on_customer(self, feeds):
        return feeds.filter(
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=self.fixture.customer.pk,
        ).exists()

    def filed_on_project(self, feeds):
        return feeds.filter(
            content_type=ContentType.objects.get_for_model(Project),
            object_id=self.project.pk,
        ).exists()

    def test_unguarded_scope_files_on_wider_feeds(self):
        self.assertFalse(utils.has_scope_event_guard(self.project))
        feeds = self.grant_role()
        self.assertTrue(self.filed_on_project(feeds))
        self.assertTrue(self.filed_on_customer(feeds))

    def test_guarded_scope_stays_off_wider_feeds_by_default(self):
        utils.register_scope_event_guard(Project, only_staff)

        self.assertTrue(utils.has_scope_event_guard(self.project))
        feeds = self.grant_role()
        self.assertTrue(self.filed_on_project(feeds))
        self.assertFalse(self.filed_on_customer(feeds))

    def test_guard_may_let_its_events_reach_wider_feeds(self):
        utils.register_scope_event_guard(Project, only_staff, wider_feeds=True)

        self.assertFalse(utils.has_scope_event_guard(self.project))
        feeds = self.grant_role()
        self.assertTrue(self.filed_on_project(feeds))
        self.assertTrue(self.filed_on_customer(feeds))
        # The scope's own feed is still guarded.
        self.assertFalse(utils.can_view_scope_events(self.fixture.owner, self.project))
        self.assertTrue(utils.can_view_scope_events(self.fixture.staff, self.project))

    def test_row_filtered_scope_stays_off_wider_feeds_by_default(self):
        utils.register_scope_event_filter(Project, own_actions)

        self.assertTrue(utils.has_scope_event_guard(self.project))
        feeds = self.grant_role()
        self.assertTrue(self.filed_on_project(feeds))
        self.assertFalse(self.filed_on_customer(feeds))

    def test_row_filter_may_let_its_events_reach_wider_feeds(self):
        utils.register_scope_event_filter(Project, own_actions, wider_feeds=True)

        self.assertFalse(utils.has_scope_event_guard(self.project))
        feeds = self.grant_role()
        self.assertTrue(self.filed_on_project(feeds))
        self.assertTrue(self.filed_on_customer(feeds))
        # The scope's own feed is still narrowed.
        self.assertIsNotNone(utils.scope_event_filter(self.fixture.owner, self.project))
        self.assertIsNone(utils.scope_event_filter(self.fixture.staff, self.project))

    def test_both_must_opt_in_for_events_to_reach_wider_feeds(self):
        utils.register_scope_event_guard(Project, only_staff, wider_feeds=True)
        utils.register_scope_event_filter(Project, own_actions)
        self.assertTrue(utils.has_scope_event_guard(self.project))

        utils.register_scope_event_filter(Project, own_actions, wider_feeds=True)
        self.assertFalse(utils.has_scope_event_guard(self.project))

        utils.register_scope_event_guard(Project, only_staff)
        self.assertTrue(utils.has_scope_event_guard(self.project))

    def test_registering_again_without_the_opt_in_withdraws_it(self):
        utils.register_scope_event_guard(Project, only_staff, wider_feeds=True)
        utils.register_scope_event_guard(Project, only_staff)
        self.assertTrue(utils.has_scope_event_guard(self.project))

        del utils._scope_event_guards[Project]
        utils.register_scope_event_filter(Project, own_actions, wider_feeds=True)
        utils.register_scope_event_filter(Project, own_actions)
        self.assertTrue(utils.has_scope_event_guard(self.project))


class ParentFeedRowFilterTest(TestCase):
    """``?scope=`` of an object that hangs off another (a ScopeMixin) lists
    the parent's feed too, but only the events of it the parent's row filter
    leaves the user."""

    def setUp(self):
        patcher = mock.patch.dict(utils._scope_event_filters, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            Invitation,
            "get_permitted_objects",
            classmethod(lambda cls, user: cls.objects.all()),
            create=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.invitation = users_factories.ProjectInvitationFactory(scope=self.project)
        self.reader = self.fixture.manager
        self.own = self.event_on(self.project, self.reader)
        self.others = self.event_on(self.project, self.fixture.owner)
        self.on_invitation = self.event_on(self.invitation, self.fixture.owner)

    def event_on(self, scope, user):
        event = logging_factories.EventFactory(context={"user_uuid": user.uuid.hex})
        logging_factories.FeedFactory(event=event, scope=scope)
        return event

    def listed(self, user):
        """Which of this test's events ``?scope=<invitation>`` lists."""
        ours = Event.objects.filter(
            pk__in=[self.own.pk, self.others.pk, self.on_invitation.pk]
        )
        return set(utils.filter_events_for_scope(user, self.invitation, ours))

    def test_parent_feed_is_listed_whole_without_a_row_filter(self):
        self.assertEqual(
            self.listed(self.reader), {self.own, self.others, self.on_invitation}
        )

    def test_parent_row_filter_narrows_the_parents_events(self):
        utils.register_scope_event_filter(Project, own_actions)

        self.assertEqual(self.listed(self.reader), {self.own, self.on_invitation})
        self.assertEqual(
            self.listed(self.fixture.staff), {self.own, self.others, self.on_invitation}
        )
