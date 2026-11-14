"""An award's links survive updates from the awarding portal that leave them out.

ManagedProject.merge_details took award, call and renewal straight from each
incoming update, so one without them removed the stored links, and an award's
"Go to award" link came and went with whichever kind of update arrived last.
"""

import json

import openportal
from django.test import TestCase

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models

AWARD = {"id": "0261-7765-7752-1", "url": "https://awards.example/projects/a/"}
CALL = {"id": "Call - round", "url": "https://awards.example/calls/c/"}


def _details(**fields):
    return openportal.AwardDetails(json.dumps(fields))


class MergeKeepsAwardLinksTest(TestCase):
    def setUp(self):
        self.managed_project = models.ManagedProject.objects.create(
            destination="remote.awards",
            identifier="award.awards",
            project=structure_fixtures.ProjectFixture().project,
            details={"name": "Award", "award": AWARD, "call": CALL},
        )

    def _link(self, details, field):
        link = getattr(details, field)
        return None if link is None else json.loads(str(link))

    def test_an_update_without_links_keeps_the_stored_ones(self):
        merged = self.managed_project.merge_details(_details(name="Award"))

        self.assertEqual(self._link(merged, "award"), AWARD)
        self.assertEqual(self._link(merged, "call"), CALL)

    def test_an_update_with_a_link_replaces_it(self):
        new_award = {"id": "0261-7765-7752-2", "url": "https://awards.example/b/"}

        merged = self.managed_project.merge_details(
            _details(name="Award", award=new_award)
        )

        self.assertEqual(self._link(merged, "award"), new_award)
        self.assertEqual(self._link(merged, "call"), CALL)

    def test_a_link_is_added_when_none_was_stored(self):
        merged = self.managed_project.merge_details(
            _details(name="Award", renewal={"url": "https://awards.example/r/"})
        )

        self.assertEqual(
            self._link(merged, "renewal")["url"], "https://awards.example/r/"
        )

    def test_alternating_updates_keep_the_link(self):
        """With link, without, with, without: it must end with the link."""
        for incoming in (
            _details(name="Award", award=AWARD),
            _details(name="Award"),
            _details(name="Award", award=AWARD),
            _details(name="Award"),
        ):
            self.managed_project.set_details(
                self.managed_project.merge_details(incoming)
            )

        self.managed_project.refresh_from_db()
        self.assertEqual(self.managed_project.details["award"], AWARD)
