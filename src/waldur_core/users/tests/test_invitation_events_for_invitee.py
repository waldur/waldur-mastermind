"""The invitee reads the events about an invitation addressed to them.

An invitation names its invitee by address, not by account, and so do its
events: anyone who may manage invitations on a scope may invite any address,
and an event naming the account with that address would tell them whether
there is one, and which. The invitee is matched as they read the events
instead, by the one address an event names (``context.invitee_email``),
ignoring case. An event that moves an invitation to another address names
both and is for neither.
"""

from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging import event_logger
from waldur_core.logging import utils as logging_utils
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.users.tests import factories
from waldur_core.users.views import UNSENT_INVITATION_STATES

EVENTS_URL = "http://testserver" + reverse("event-list")
ADDRESS = "invitee@example.com"
INVITATION_EVENTS = [
    EventType.USER_INVITATION_UPDATED,
    EventType.USER_INVITATION_DELETED,
]


class InvitationEventsForInviteeTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.owner = self.fixture.owner
        # Manages the organisation's invitations.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.invitee = structure_factories.UserFactory(email=ADDRESS)
        self.bystander = structure_factories.UserFactory()
        self.invitation = factories.CustomerInvitationFactory(
            scope=self.fixture.customer, email=ADDRESS.upper()
        )
        self.url = factories.CustomerInvitationFactory.get_url(self.invitation)

    def as_owner(self):
        self.client.force_authenticate(self.owner)

    def delete(self):
        self.as_owner()
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        return Event.objects.get(event_type=EventType.USER_INVITATION_DELETED)

    def update(self, **payload):
        self.as_owner()
        response = self.client.patch(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return Event.objects.get(event_type=EventType.USER_INVITATION_UPDATED)

    def own_feed(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL, {"related_user_uuid": user.uuid.hex, "page_size": 200}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def assert_names_no_account(self, event, user):
        for key in event.context:
            self.assertFalse(key.startswith("affected_user"), key)
        self.assertNotIn(user.uuid.hex, str(event.context))
        self.assertNotIn(user.uuid.hex, event.message)
        self.assertNotIn(user.username, event.context.values())

    def test_the_events_name_the_address_and_no_account(self):
        for event in (self.update(role=CustomerRole.SUPPORT.uuid.hex), self.delete()):
            with self.subTest(event_type=event.event_type):
                self.assertEqual(event.context["invitee_email"], ADDRESS.upper())
                self.assert_names_no_account(event, self.invitee)

    def test_the_owner_learns_no_account_by_inviting_its_address(self):
        """An owner who invites the address of a user of another organisation
        and deletes the invitation learns nothing of that user's account
        from the event, read as an owner reads it."""
        stranger = structure_factories.UserFactory(email="stranger@example.com")
        structure_factories.CustomerFactory().add_user(stranger, CustomerRole.OWNER)
        self.invitation = factories.CustomerInvitationFactory(
            scope=self.fixture.customer, email=stranger.email
        )
        self.url = factories.CustomerInvitationFactory.get_url(self.invitation)
        event = self.delete()
        self.assert_names_no_account(event, stranger)

        self.client.force_authenticate(self.owner)
        response = self.client.get(
            EVENTS_URL,
            {
                "scope": structure_factories.CustomerFactory.get_url(
                    self.fixture.customer
                ),
                "event_type": EventType.USER_INVITATION_DELETED,
                "page_size": 200,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["uuid"] for item in response.data], [event.uuid.hex])
        self.assertNotIn(stranger.uuid.hex, str(response.data))

    def test_the_invitee_reads_them_in_their_own_feed_and_no_one_else_does(self):
        updated = self.update(role=CustomerRole.SUPPORT.uuid.hex)
        deleted = self.delete()
        expected = {updated.uuid.hex, deleted.uuid.hex}

        self.assertTrue(expected <= self.own_feed(self.invitee))
        self.assertFalse(expected & self.own_feed(self.bystander))

    def test_an_invitation_never_sent_is_not_addressed_to_its_invitee(self):
        """A request awaiting staff approval, or one staff refused, was never
        mailed to its address (and cannot be edited): deleting it logs an
        event that stays off the invitee's feed."""
        for state in UNSENT_INVITATION_STATES:
            with self.subTest(state=state):
                Event.objects.all().delete()
                self.invitation = factories.CustomerInvitationFactory(
                    scope=self.fixture.customer, email=ADDRESS, state=state
                )
                self.url = factories.CustomerInvitationFactory.get_url(self.invitation)
                deleted = self.delete()
                self.assertNotIn("invitee_email", deleted.context)
                self.assertNotIn(deleted.uuid.hex, self.own_feed(self.invitee))

    def test_a_user_with_no_email_is_addressed_by_no_invitation(self):
        no_email = structure_factories.UserFactory(email="")
        event_logger.emit(
            "Invitation to {invitation_email} has been deleted.",
            event_type=EventType.USER_INVITATION_DELETED,
            event_context={"invitation_email": "", "invitee_email": ""},
            scopes=[self.fixture.customer],
        )
        self.assertTrue(Event.objects.filter(context__invitee_email="").exists())

        self.assertFalse(
            Event.objects.filter(
                logging_utils.invitation_events_addressed_to(no_email)
            ).exists()
        )
        self.assertFalse(
            {
                event.uuid.hex
                for event in Event.objects.filter(event_type__in=INVITATION_EVENTS)
            }
            & self.own_feed(no_email)
        )

    def test_an_invitation_moved_to_another_address_is_for_neither(self):
        new_holder = structure_factories.UserFactory(email="moved@example.com")
        event = self.update(email=new_holder.email)

        # It names both addresses, the former as the invitation's.
        self.assertEqual(event.context["invitation_email"], ADDRESS.upper())
        self.assertIn(new_holder.email, event.message)
        self.assertNotIn("invitee_email", event.context)
        for user in (self.invitee, new_holder):
            with self.subTest(user=user.email):
                self.assertNotIn(event.uuid.hex, self.own_feed(user))
                self.assert_names_no_account(event, user)
