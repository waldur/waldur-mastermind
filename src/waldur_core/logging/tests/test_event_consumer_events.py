"""The event-consumer audit type must be reachable from an audit view.

Registering an EventType is not enough: the audit-log views filter by group
(``/api/events/?feature=<group>``), so a type in no group is written to the
database and never seen. The orphan guard in passkeys/tests/test_events.py only
covers the auth_/pat_/passkey_ prefixes, so this type needs its own.
"""

from django.test import SimpleTestCase

from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup, EventType

EVENT_CONSUMER_EVENTS = [
    EventType.EVENT_CONSUMER_REGISTERED_WITH_BROAD_CREDENTIAL,
]


class EventConsumerEventGroupTest(SimpleTestCase):
    def test_every_event_consumer_event_is_in_the_auth_group(self):
        for event in EVENT_CONSUMER_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, EVENT_GROUP_MAPPING[EventGroup.AUTH])

    def test_every_event_consumer_event_is_in_the_users_group(self):
        """These events are scoped to the consumer's owner, whose profile audit
        log queries feature=users."""
        for event in EVENT_CONSUMER_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, EVENT_GROUP_MAPPING[EventGroup.USERS])

    def test_no_event_consumer_event_is_orphaned(self):
        """Catch a newly added event_consumer_ type that nobody put in a group."""
        grouped = {e for types in EVENT_GROUP_MAPPING.values() for e in types}
        orphans = [
            e.value
            for e in EventType
            if e.value.startswith("event_consumer_") and e not in grouped
        ]
        self.assertEqual(orphans, [])
