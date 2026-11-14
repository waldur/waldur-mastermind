import datetime
import json

import openportal
from django.test import TestCase

from waldur_openportal.tests.test_award_membership import MembershipSyncFixture

STORED_START = datetime.date(2026, 12, 1)
PROJECT_START = datetime.date(2026, 11, 20)


def details(**fields):
    payload = {
        key: value.isoformat() if isinstance(value, datetime.date) else value
        for key, value in fields.items()
    }
    return openportal.AwardDetails(json.dumps(payload))


class StartDateFromMergedDetailsTest(MembershipSyncFixture, TestCase):
    """
    update_award checks the merged details' start date against the project,
    and must also take the value from them: an update that omits the start
    date must not clear the project's.
    """

    def setUp(self):
        super().setUp()
        self.project.start_date = PROJECT_START

    def merge_with_stored_start(self, incoming):
        merged = json.loads(incoming.to_json())
        if merged.get("start_date") is None:
            merged["start_date"] = STORED_START.isoformat()
        return openportal.AwardDetails(json.dumps(merged))

    def test_update_without_a_start_date_applies_the_merged_one(self):
        self.managed_project.merge_details.side_effect = self.merge_with_stored_start
        incoming = details(description="update without dates")
        self.assertIsNone(incoming.start_date)

        self.update(incoming)

        self.assertEqual(self.project.start_date, STORED_START)
        self.assert_saved_with_start_date()

    def test_update_with_a_start_date_applies_it(self):
        new_start = datetime.date(2027, 1, 15)

        self.update(details(start_date=new_start))

        self.assertEqual(self.project.start_date, new_start)
        self.assert_saved_with_start_date()

    def assert_saved_with_start_date(self):
        self.project.save.assert_called_once()
        self.assertIn("start_date", self.project.save.call_args.kwargs["update_fields"])
