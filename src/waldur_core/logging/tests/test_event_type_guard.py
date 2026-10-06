from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging import tasks, utils
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories

GUARDED = "guarded_event"
EVENTS_URL = "http://testserver" + reverse("event-list")


class EventTypeGuardTest(test.APITestCase):
    """An event type may be withheld from some readers of the feed it is on."""

    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.owner = self.fixture.owner
        self.guarded = factories.EventFactory(event_type=GUARDED)
        self.plain = factories.EventFactory(event_type="plain_event")
        for event in (self.guarded, self.plain):
            factories.FeedFactory(scope=self.customer, event=event)
        self.hidden = True
        patcher = mock.patch.dict(utils._event_type_guards, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        utils.register_event_type_guard(GUARDED, self.hidden_customers)

    def hidden_customers(self, user):
        if user.is_staff or not self.hidden:
            return None
        return Customer.objects.filter(id=self.customer.id)

    def listed(self, user, path="", event_type=(GUARDED, "plain_event")):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL + path,
            {
                "scope": structure_factories.CustomerFactory.get_url(self.customer),
                "event_type": list(event_type),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def hook_matches(self, user, event):
        hook = factories.WebHookFactory(user=user, event_types=[event.event_type])
        return tasks.check_event(event, hook)

    def test_hidden_event_is_left_out_of_the_feed_and_its_count(self):
        self.assertEqual(
            [e["uuid"] for e in self.listed(self.owner)], [self.plain.uuid.hex]
        )
        self.assertEqual(self.listed(self.owner, event_type=[GUARDED]), [])
        self.assertEqual(self.listed(self.owner, "count/")["count"], 1)

    def test_hidden_event_does_not_reach_the_reader_by_hook(self):
        self.assertFalse(self.hook_matches(self.owner, self.guarded))
        self.assertTrue(self.hook_matches(self.owner, self.plain))

    def test_event_is_shown_once_the_guard_lets_it_through(self):
        self.hidden = False

        self.assertEqual(len(self.listed(self.owner)), 2)
        self.assertTrue(self.hook_matches(self.owner, self.guarded))

    def test_reader_the_guard_does_not_restrict_sees_it(self):
        staff = structure_factories.UserFactory(is_staff=True)

        self.assertEqual(len(self.listed(staff, event_type=[GUARDED])), 1)
        self.assertTrue(self.hook_matches(staff, self.guarded))
