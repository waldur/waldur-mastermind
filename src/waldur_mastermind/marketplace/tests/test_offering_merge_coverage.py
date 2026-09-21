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

    def test_every_entry_declares_an_area_and_derives_an_effect(self):
        for label, entry in coverage.MERGE_COVERAGE.items():
            self.assertIn(entry.area, coverage.AREAS, label)
            self.assertTrue(entry.area_title, label)
            if entry.strategy == coverage.NOT_APPLICABLE:
                self.assertFalse(entry.can_list_rows, label)
                self.assertRaises(ValueError, lambda: entry.effect)  # noqa: B023
                continue
            self.assertIn(entry.effect, coverage.EFFECTS, label)
            self.assertTrue(entry.effect_title, label)

    def test_an_entry_without_a_known_area_is_refused(self):
        entry = coverage.CoverageEntry(
            "marketplace.Resource.offering",
            coverage.REPOINT,
            "Reason.",
            area="nowhere",
        )
        with self.assertRaises(ValueError):
            coverage._entries(entry)

    def test_the_effect_of_a_strategy_is_what_the_ui_shows(self):
        effects = {
            "marketplace.Resource.offering": coverage.EFFECT_MOVED,
            # A repointed JSON document does not move: its keys are renamed.
            "marketplace.Resource.limits": coverage.EFFECT_REWRITTEN,
            "marketplace.OfferingUser.offering": coverage.EFFECT_DEDUPLICATED,
            "invoices.InvoiceItem.details": coverage.EFFECT_REWRITTEN,
            "marketplace.ComponentUsageMonthly.component": coverage.EFFECT_RECOMPUTED,
            "marketplace.Plan.offering": coverage.EFFECT_KEPT_ON_SOURCE,
        }
        for label, effect in effects.items():
            self.assertEqual(coverage.MERGE_COVERAGE[label].effect, effect, label)

    def test_only_recomputed_and_inapplicable_entries_refuse_a_row_listing(self):
        for label, entry in coverage.MERGE_COVERAGE.items():
            expected = entry.strategy not in (
                coverage.RECOMPUTE,
                coverage.NOT_APPLICABLE,
            )
            self.assertEqual(entry.can_list_rows, expected, label)

    def test_areas_group_the_registry_into_what_staff_recognise(self):
        by_area = {}
        for entry in coverage.MERGE_COVERAGE.values():
            by_area.setdefault(entry.area, []).append(entry.label)
        self.assertEqual(set(by_area), set(coverage.AREAS))
        self.assertIn(
            "marketplace.Order.offering", by_area[coverage.AREA_RESOURCES_AND_ORDERS]
        )
        self.assertIn(
            "marketplace.ComponentUsage.component",
            by_area[coverage.AREA_BILLING_HISTORY],
        )
        self.assertIn("invoices.InvoiceItem.details", by_area[coverage.AREA_INVOICES])
        self.assertIn(
            "permissions.UserRole.scope", by_area[coverage.AREA_ACCOUNTS_AND_ACCESS]
        )
        self.assertIn(
            "marketplace.Screenshot.offering",
            by_area[coverage.AREA_OFFERING_CONFIGURATION],
        )
