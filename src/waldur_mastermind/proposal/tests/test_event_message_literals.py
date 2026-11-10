"""Names and reasons show in reviewer registry event messages as written."""

from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import reviewer_registry
from waldur_mastermind.proposal.enums import ReviewerStandings

from . import factories

TEXT = "AA{{BB}}CC {nosuchkey} {"


def last_message(event_type):
    return Event.objects.filter(event_type=event_type).latest("created").message


class ReviewerStandingMessageTest(test.APITestCase):
    def test_names_and_reason_show_as_written(self):
        profile = factories.ReviewerProfileFactory(
            user__first_name="AA{{BB}}CC", user__last_name="{nosuchkey}"
        )
        staff = structure_factories.UserFactory(
            is_staff=True, first_name="Staff", last_name="{"
        )

        reviewer_registry.set_standing(
            profile, ReviewerStandings.SUSPENDED, TEXT, staff
        )

        self.assertEqual(
            last_message(EventType.REVIEWER_STANDING_CHANGED),
            "Standing of reviewer AA{{BB}}CC {nosuchkey} changed from active to "
            "suspended by Staff {: " + TEXT,
        )
