from django.test import SimpleTestCase

from waldur_mastermind.marketplace import offering_merge_coverage as coverage


class MergeCoverageRegistryTest(SimpleTestCase):
    def test_every_relation_to_merged_models_has_an_entry(self):
        uncovered = coverage.find_uncovered_relations()
        self.assertEqual(
            uncovered,
            [],
            "These relations reference Offering, Plan, OfferingComponent or "
            "PlanComponent but have no entry in MERGE_COVERAGE "
            "(marketplace/offering_merge_coverage.py). Decide what an offering "
            "merge does with them and add an entry.",
        )

    def test_no_entry_is_stale(self):
        self.assertEqual(coverage.find_stale_entries(), [])

    def test_a_missing_entry_is_reported(self):
        registry = dict(coverage.MERGE_COVERAGE)
        del registry["marketplace.Resource.offering"]
        del registry["invoices.CustomerCredit.offerings"]
        self.assertEqual(
            coverage.find_uncovered_relations(registry),
            ["invoices.CustomerCredit.offerings", "marketplace.Resource.offering"],
        )

    def test_relations_hidden_by_related_name_plus_are_discovered(self):
        # Resource.offering and Order.offering use related_name="+", which
        # _meta.related_objects alone would miss.
        discovered = coverage.discover_relations()
        self.assertIn("marketplace.Resource.offering", discovered)
        self.assertIn("marketplace.Order.offering", discovered)

    def test_generic_and_uuid_references_are_listed(self):
        for label in (
            "permissions.UserRole.scope",
            "permissions.RoleAvailability.scope",
            "permissions.CustomerRoleConcealment.scope",
            "checklist.ChecklistCompletion.scope",
            "logging.EventConsumerScope.scope",
            "logging.EventSubscriptionQueue.offering_uuid",
            "waldur_pid.DataciteReferral.scope",
        ):
            self.assertIn(label, coverage.MERGE_COVERAGE)

    def test_every_entry_is_explained_and_resolvable(self):
        for label, entry in coverage.MERGE_COVERAGE.items():
            self.assertTrue(entry.reason, label)
            self.assertIn(entry.strategy, coverage.STRATEGIES, label)
            if entry.kind != coverage.GENERIC:
                entry.model._meta.get_field(entry.field_name)
