"""A call's event feed records its modifications: settings and documents,
who was given or lost which role on the call, invitations, the rounds'
history. All of it is for the call team -- the team the round events' guard
admits, and whoever may change who holds which role on the call. Anyone else
who sees the call -- its reviewers and panel members -- reads only the events
about them or done by them, by the API or by hook. The managing
organisation's feed still records the call's team changes for its owners."""

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging import event_logger
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.permissions.utils import has_permission
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models as users_models
from waldur_core.users.tests import factories as users_factories
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.tests import factories, fixtures

EVENTS_URL = "http://testserver" + reverse("event-list")
ROLE_EVENTS = {EventType.ROLE_GRANTED, EventType.ROLE_REVOKED}


class CallEventFeedTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.reviewer = self.fixture.reviewer_1
        self.other_reviewer = self.fixture.reviewer_2
        self.panel_member = self.fixture.panel_member
        self.call_manager = self.fixture.call_manager
        # A call reviewer and a panel member see the call itself.
        CallRole.PANEL_MEMBER.add_permission(PermissionEnum.LIST_CALLS)
        # Another reviewer leaves the call: the feed records both changes.
        self.call.remove_user(self.other_reviewer, CallRole.REVIEWER)
        # A change to the call's documents, by its manager.
        self.managers_change = self.act(self.call_manager)
        # A round's history and an invitation to the call.
        utils.emit_round_event(
            self.fixture.round,
            "Round {round_name} of call {call_name} has opened a step.",
            EventType.ROUND_STEP_OPENED,
        )
        self.round_event = Event.objects.get(event_type=EventType.ROUND_STEP_OPENED)
        event_logger.emit(
            "Invitation to {invitation_email} for {scope_name} has been deleted.",
            event_type=EventType.USER_INVITATION_DELETED,
            event_context={
                "invitation_email": "someone@example.com",
                "scope_name": self.call.name,
                "user": self.call_manager,
                "scope": self.call,
            },
            scopes=[self.call],
        )
        self.invitation_event = Event.objects.get(
            event_type=EventType.USER_INVITATION_DELETED
        )

    def act(self, user):
        """An event on the call's feed done by ``user``."""
        event_logger.emit(
            "Attachment for call {call_name} has been added.",
            event_type=EventType.CALL_DOCUMENT_ADDED,
            event_context={"call_name": self.call.name, "user": user},
            scopes=[self.call],
        )
        return Event.objects.filter(
            event_type=EventType.CALL_DOCUMENT_ADDED,
            context__user_uuid=user.uuid.hex,
        ).latest("created")

    def role_events_about(self, user):
        return Event.objects.filter(
            event_type__in=ROLE_EVENTS, context__affected_user_uuid=user.uuid.hex
        )

    def org_role(self, *permissions):
        """A user holding a role with ``permissions`` on the call's managing
        organisation, and nothing on the call."""
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(
                models.CallManagingOrganisation
            )
        )
        for permission in (PermissionEnum.LIST_CALLS, *permissions):
            role.add_permission(permission)
        user = structure_factories.UserFactory()
        self.fixture.manager.add_user(user, role)
        return user

    def read(self, user, scope_url=None, **params):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL,
            {
                "scope": scope_url or factories.CallFactory.get_public_url(self.call),
                "page_size": 200,
                **params,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def uuids(self, events):
        return {
            event["uuid"] if isinstance(event, dict) else event.uuid.hex
            for event in events
        }

    def call_feed(self):
        return Event.objects.filter(
            feed__content_type=ContentType.objects.get_for_model(models.Call),
            feed__object_id=self.call.pk,
        )

    def assert_reads_the_whole_feed(self, user):
        events = self.uuids(self.read(user))
        self.assertEqual(events, self.uuids(self.call_feed()))
        # Round and invitation events among them.
        self.assertIn(self.round_event.uuid.hex, events)
        self.assertIn(self.invitation_event.uuid.hex, events)

    def test_reviewer_reads_only_the_events_about_them_or_done_by_them(self):
        own_action = self.act(self.reviewer)
        expected = self.uuids([*self.role_events_about(self.reviewer), own_action])
        self.assertTrue(expected)

        for scope_url in (
            factories.CallFactory.get_public_url(self.call),
            factories.CallFactory.get_protected_url(self.call),
        ):
            with self.subTest(scope_url=scope_url):
                events = self.read(self.reviewer, scope_url)
                self.assertEqual(self.uuids(events), expected)
                # Neither another reviewer's role changes nor the manager's
                # change to the call.
                self.assertNotIn(self.other_reviewer.uuid.hex, str(events))
                self.assertNotIn(self.managers_change.uuid.hex, self.uuids(events))

        self.client.force_authenticate(self.reviewer)
        count = self.client.get(
            EVENTS_URL + "count/",
            {"scope": factories.CallFactory.get_public_url(self.call)},
        )
        self.assertEqual(count.data["count"], len(expected))

    def test_reviewer_reads_their_own_role_granted_and_revoked(self):
        self.call.remove_user(self.reviewer, CallRole.REVIEWER)
        self.call.add_user(self.reviewer, CallRole.REVIEWER)

        events = self.read(self.reviewer, event_type=sorted(ROLE_EVENTS))
        self.assertEqual(
            self.uuids(events), self.uuids(self.role_events_about(self.reviewer))
        )
        self.assertEqual({event["event_type"] for event in events}, ROLE_EVENTS)

    def test_panel_member_reads_only_the_events_about_them(self):
        events = self.read(self.panel_member)
        self.assertEqual(
            self.uuids(events), self.uuids(self.role_events_about(self.panel_member))
        )
        self.assertNotIn(self.reviewer.uuid.hex, str(events))
        self.assertNotIn(self.other_reviewer.uuid.hex, str(events))

    def test_reviewer_and_panel_member_read_neither_round_events_nor_others_invitations(
        self,
    ):
        for user in (self.reviewer, self.panel_member):
            with self.subTest(user=user.username):
                events = self.uuids(self.read(user))
                self.assertNotIn(self.round_event.uuid.hex, events)
                self.assertNotIn(self.invitation_event.uuid.hex, events)

    def invite(self, user):
        """An invitation to the call addressed to ``user``'s email."""
        return users_models.Invitation.objects.create(
            email=user.email.upper(),
            scope=self.call,
            customer=self.call.manager.customer,
            role=CallRole.REVIEWER,
            created_by=self.call_manager,
        )

    def test_reviewer_reads_the_invitation_events_addressed_to_them(self):
        """A reviewer reads the events about an invitation to the call
        addressed to their email, matched as they read them; not another
        reviewer's, and not one moved between their addresses. The events
        name no account."""
        own = self.invite(self.reviewer)
        others = self.invite(self.other_reviewer)
        self.client.force_authenticate(self.fixture.staff)
        for invitation in (own, others):
            url = users_factories.CustomerInvitationFactory.get_url(invitation)
            response = self.client.patch(url, {"role": CallRole.PANEL_MEMBER.uuid.hex})
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            response = self.client.delete(url)
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        # Moved from the other reviewer's address to the reviewer's: the
        # event names both addresses, so it is for neither.
        moved = self.invite(self.other_reviewer)
        response = self.client.patch(
            users_factories.CustomerInvitationFactory.get_url(moved),
            {"email": self.reviewer.email},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        invitation_events = Event.objects.filter(
            event_type__in=[
                EventType.USER_INVITATION_UPDATED,
                EventType.USER_INVITATION_DELETED,
            ]
        )
        for event in invitation_events:
            self.assertNotIn("affected_user_uuid", event.context)
        about_reviewer = invitation_events.filter(
            context__invitee_email__iexact=self.reviewer.email
        )
        self.assertEqual(about_reviewer.count(), 2)

        # Besides those: the setUp one, the other reviewer's two and the
        # moved one.
        self.assertEqual(invitation_events.count(), 6)
        events = self.uuids(self.read(self.reviewer))
        self.assertEqual(
            self.uuids(invitation_events) & events, self.uuids(about_reviewer)
        )

        self.assertFalse(
            self.uuids(invitation_events) & self.uuids(self.read(self.panel_member))
        )
        self.assert_reads_the_whole_feed(self.call_manager)

    def owner_as_shipped(self):
        """The customer's owner, holding on the customer what permissions.yaml
        gives CUSTOMER.OWNER for calls: no CALL.UPDATE, but who holds which
        role on the call, closing rounds and deciding proposals."""
        for permission in (
            PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
            PermissionEnum.CLOSE_ROUNDS,
            PermissionEnum.CREATE_CALL_PERMISSION,
            PermissionEnum.UPDATE_CALL_PERMISSION,
            PermissionEnum.DELETE_CALL_PERMISSION,
        ):
            CustomerRole.OWNER.add_permission(permission)
        owner = self.fixture.owner
        self.assertFalse(
            has_permission(owner, PermissionEnum.UPDATE_CALL, self.fixture.customer)
        )
        return owner

    def test_the_call_team_staff_and_support_read_the_whole_feed(self):
        readers = {
            "call manager": self.call_manager,
            "call organiser": self.fixture.call_organizer_user,
            "edits the call on its organisation": self.org_role(
                PermissionEnum.UPDATE_CALL
            ),
            "decides the call's proposals on its organisation": self.org_role(
                PermissionEnum.APPROVE_AND_REJECT_PROPOSALS
            ),
            "customer owner": self.owner_as_shipped(),
            "staff": self.fixture.staff,
            "support": structure_factories.UserFactory(is_support=True),
        }
        for label, user in readers.items():
            with self.subTest(reader=label):
                self.assert_reads_the_whole_feed(user)
        # The whole feed includes the role changes naming another reviewer.
        self.assertIn(self.other_reviewer.uuid.hex, str(self.read(self.call_manager)))

    def test_one_call_team_for_the_feed_and_the_round_events(self):
        cases = {
            "call manager": (self.call_manager, True),
            "call organiser": (self.fixture.call_organizer_user, True),
            "decider": (
                self.org_role(PermissionEnum.APPROVE_AND_REJECT_PROPOSALS),
                True,
            ),
            "customer owner": (self.owner_as_shipped(), True),
            "reviewer": (self.reviewer, False),
            "panel member": (self.panel_member, False),
        }
        for label, (user, on_team) in cases.items():
            with self.subTest(user=label):
                self.assertEqual(
                    logging_utils.scope_event_filter(user, self.call) is None,
                    on_team,
                )
                self.assertEqual(
                    logging_utils.can_view_event(user, self.round_event), on_team
                )

    def test_reviewer_who_also_manages_the_call_reads_the_whole_feed(self):
        self.call.add_user(self.reviewer, CallRole.MANAGER)
        self.assert_reads_the_whole_feed(self.reviewer)

    def test_role_changes_stay_on_the_managing_organisations_feed(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        for event_type in ROLE_EVENTS:
            with self.subTest(event_type=event_type):
                self.assertTrue(
                    Feed.objects.filter(
                        event__event_type=event_type,
                        event__context__affected_user_uuid=self.other_reviewer.uuid.hex,
                        content_type=customer_ct,
                        object_id=self.fixture.customer.pk,
                    ).exists()
                )
        events = self.read(
            self.fixture.owner,
            structure_factories.CustomerFactory.get_url(self.fixture.customer),
        )
        self.assertLessEqual(ROLE_EVENTS, {event["event_type"] for event in events})
        self.assertIn(self.other_reviewer.uuid.hex, str(events))

    def test_hooks_deliver_what_the_listing_shows(self):
        own_action = self.act(self.reviewer)
        reviewers_grant = self.role_events_about(self.reviewer).get()
        others_grant = self.role_events_about(self.other_reviewer).get(
            event_type=EventType.ROLE_GRANTED
        )
        panel_grant = self.role_events_about(self.panel_member).get()
        cases = (
            (self.reviewer, reviewers_grant, True),
            (self.reviewer, own_action, True),
            (self.reviewer, others_grant, False),
            (self.reviewer, self.managers_change, False),
            (self.panel_member, panel_grant, True),
            (self.panel_member, reviewers_grant, False),
            (self.panel_member, own_action, False),
            (self.call_manager, others_grant, True),
            (self.call_manager, own_action, True),
            (self.call_manager, self.managers_change, True),
        )
        for hook_factory in (
            logging_factories.WebHookFactory,
            logging_factories.EmailHookFactory,
        ):
            for user, event, delivered in cases:
                hook = hook_factory(user=user, event_types=[event.event_type])
                with self.subTest(
                    hook=hook_factory.__name__,
                    user=user.username,
                    event=event.event_type,
                ):
                    self.assertEqual(logging_tasks.check_event(event, hook), delivered)

    def test_call_feed_is_narrowed_but_its_events_reach_wider_feeds(self):
        self.assertFalse(logging_utils.has_scope_event_guard(self.call))
        self.assertIsNone(
            logging_utils.scope_event_filter(self.call_manager, self.call)
        )
        self.assertIsNotNone(logging_utils.scope_event_filter(self.reviewer, self.call))

    def test_review_and_proposal_events_stay_off_the_organisations_feed(self):
        self.assertTrue(logging_utils.has_scope_event_guard(self.fixture.review))
        self.assertTrue(logging_utils.has_scope_event_guard(self.fixture.proposal))
