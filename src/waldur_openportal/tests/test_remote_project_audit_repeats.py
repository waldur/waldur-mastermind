from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import models
from waldur_openportal.remote_project_service import _get_or_create_audit_entry

UPDATED = models.RemoteProjectAuditEventType.AWARD_UPDATED
STATE = models.RemoteProjectAuditEventType.STATE_CHANGED


class RemoteProjectAuditRepeatTest(TestCase):
    def setUp(self):
        self.remote_project = models.RemoteProject.objects.create(
            destination="a.b.c",
            identifier="p.q",
            state=models.RemoteProjectState.PENDING,
            current_project=structure_factories.ProjectFactory(),
        )

    def record(self, event_type=UPDATED, **kwargs):
        return _get_or_create_audit_entry(self.remote_project, event_type, **kwargs)

    def count(self):
        return models.RemoteProjectAuditEntry.objects.count()

    def test_resend_of_same_details_is_not_a_new_entry(self):
        first = self.record(new_details={"name": "x"}, previous_details={"v": 1})
        again = self.record(new_details={"name": "x"}, previous_details={"v": 2})

        self.assertEqual(again, first)
        self.assertEqual(self.count(), 1)

    def test_changed_details_are_a_new_entry(self):
        self.record(new_details={"name": "x"})
        self.record(new_details={"name": "y"})

        self.assertEqual(self.count(), 2)

    def test_new_allocation_ledger_entry_gets_its_own_entry(self):
        self.record(new_details={"allocation": "10 GPUh"})
        ledger = models.RemoteProjectAllocationEntry.objects.create(
            remote_project=self.remote_project, allocation=10
        )
        self.record(new_details={"allocation": "10 GPUh"}, allocation_entry=ledger)

        self.assertEqual(self.count(), 2)

    def test_previous_details_still_count_without_new_details(self):
        self.record(STATE, previous_details={"v": 1}, note="n")
        self.record(STATE, previous_details={"v": 2}, note="n")

        self.assertEqual(self.count(), 2)
