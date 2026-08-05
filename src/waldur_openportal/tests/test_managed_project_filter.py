import datetime

from django.test import TestCase
from django.utils import timezone

from waldur_openportal import models
from waldur_openportal.filters import ManagedProjectFilter

DESTINATION = "someportal.somebridge.someoffering"


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class HideEmbargoedFilterTest(TestCase):
    """
    Embargo has to be filtered in the database: doing it on the returned page
    leaves the result count and the page contents disagreeing with each other.
    """

    def setUp(self):
        now = timezone.now()

        self.future = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier="future.someportal",
            details={"earliest_approve": iso(now + datetime.timedelta(days=7))},
        )
        self.past = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier="past.someportal",
            details={"earliest_approve": iso(now - datetime.timedelta(days=7))},
        )
        self.explicit_null = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier="null.someportal",
            details={"earliest_approve": None},
        )
        self.absent = models.ManagedProject.objects.create(
            destination=DESTINATION,
            identifier="absent.someportal",
            details={"name": "No embargo key at all"},
        )

    def filtered(self, data):
        return set(
            ManagedProjectFilter(
                data, queryset=models.ManagedProject.objects.all()
            ).qs.values_list("identifier", flat=True)
        )

    def test_embargoed_award_is_dropped(self):
        self.assertEqual(
            self.filtered({"hide_embargoed": "true"}),
            {"past.someportal", "null.someportal", "absent.someportal"},
        )

    def test_nothing_is_dropped_when_the_filter_is_off(self):
        self.assertEqual(
            self.filtered({"hide_embargoed": "false"}),
            {
                "future.someportal",
                "past.someportal",
                "null.someportal",
                "absent.someportal",
            },
        )

    def test_nothing_is_dropped_when_the_filter_is_absent(self):
        self.assertEqual(len(self.filtered({})), 4)
