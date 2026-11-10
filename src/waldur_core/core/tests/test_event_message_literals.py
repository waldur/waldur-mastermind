"""Text people write shows in user event messages as written."""

from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories

TEXT = "AA{{BB}}CC {nosuchkey} {"


def last_message(event_type):
    return Event.objects.filter(event_type=event_type).latest("created").message


class UserUpdateMessageTest(test.APITestCase):
    def test_details_and_source_show_as_written(self):
        user = factories.UserFactory(description="old")
        user.description = TEXT
        user._change_source = f"sync {TEXT}"
        user.save()

        message = last_message(EventType.USER_UPDATE_SUCCEEDED)
        self.assertIn(f"Source: sync {TEXT}.", message)
        self.assertIn(f"description: old -> {TEXT}", message)
