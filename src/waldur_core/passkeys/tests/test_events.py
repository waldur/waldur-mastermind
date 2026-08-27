"""Passkey events must be reachable from the views that people actually read.

Registering an event type is not enough: the audit-log views filter by group
(``/api/events/?feature=<group>``), so a type in no group the UI asks for is
written to the database and never seen. That is exactly how the passkey events
were invisible on the profile audit log at first.
"""

from django.test import SimpleTestCase

from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup, EventType

PASSKEY_EVENTS = [
    EventType.PASSKEY_REGISTERED,
    EventType.PASSKEY_RENAMED,
    EventType.PASSKEY_REVOKED,
    EventType.PASSKEY_AUTHENTICATION_SUCCEEDED,
    EventType.PASSKEY_AUTHENTICATION_FAILED,
]


class PasskeyEventGroupTest(SimpleTestCase):
    def test_every_passkey_event_is_in_the_auth_group(self):
        for event in PASSKEY_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, EVENT_GROUP_MAPPING[EventGroup.AUTH])

    def test_every_passkey_event_is_in_the_users_group(self):
        """The profile audit log queries feature=users.

        Without this the events exist but the page a user goes to read them is
        empty.
        """
        for event in PASSKEY_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, EVENT_GROUP_MAPPING[EventGroup.USERS])


class AuthEventVisibilityTest(SimpleTestCase):
    """Account-level events must reach the account holder's own audit log."""

    def test_every_auth_event_is_visible_to_the_user_it_concerns(self):
        """AUTH is a subset of USERS.

        Each AUTH event is about one account. The profile audit log queries
        feature=users, so an AUTH event missing from USERS is written, scoped
        to the user, and then hidden from the only page they would read it on.
        """
        missing = [
            e
            for e in EVENT_GROUP_MAPPING[EventGroup.AUTH]
            if e not in EVENT_GROUP_MAPPING[EventGroup.USERS]
        ]
        self.assertEqual(missing, [])

    def test_users_group_has_no_duplicates(self):
        users = EVENT_GROUP_MAPPING[EventGroup.USERS]
        self.assertEqual(len(users), len(set(users)))

    def test_no_authentication_event_is_orphaned(self):
        """An event in no group is invisible in every audit view.

        auth_logged_in_with_oauth was exactly that: defined, emitted on every
        OIDC login, and in no group at all.
        """
        grouped = {e for types in EVENT_GROUP_MAPPING.values() for e in types}
        orphans = [
            e.value
            for e in EventType
            if e.value.startswith(("auth_", "pat_", "passkey_")) and e not in grouped
        ]
        self.assertEqual(orphans, [])

    def test_no_passkey_event_is_orphaned(self):
        """Catch a newly added passkey event that nobody put in a group."""
        grouped = {e for types in EVENT_GROUP_MAPPING.values() for e in types}
        orphans = [
            e for e in EventType if e.value.startswith("passkey_") and e not in grouped
        ]
        self.assertEqual(orphans, [])
