"""Regression coverage for marketplace migration 0226_alter_offeringcomponent_limit_period.

The original 0226 backfill blindly rewrote every NULL/empty
OfferingComponent.limit_period to "month". On deployments where SLURM offerings
relied on the implicit policy.period contract (component.limit_period left
NULL, SlurmPeriodicUsagePolicy.period = MONTH_3 / MONTH_12), this silently
flipped quarterly/annual offerings to monthly — see WAL-9907.

These tests exercise the migration's RunPython callable directly with real
models, following the same pattern as
invoices/tests/test_non_billable_migration.py.
"""

import importlib.util
import pathlib

from django.apps import apps as django_apps
from django.test import TestCase

from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.tests.factories import SlurmPeriodicUsagePolicyFactory

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "0226_alter_offeringcomponent_limit_period.py"
)


def _load_migration_module():
    """Import the 0226 migration by file path (digit-prefixed module names
    can't be imported with a normal ``import`` statement)."""
    spec = importlib.util.spec_from_file_location(
        "_migration_0226_under_test", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Migration0226BackfillTest(TestCase):
    """The backfill must respect SlurmPeriodicUsagePolicy.period when present.

    Pre-migration state we are simulating: an OfferingComponent with NULL/empty
    limit_period belonging to an offering whose SlurmPeriodicUsagePolicy was
    explicitly set to a non-monthly period. The post-migration limit_period
    must mirror the policy intent, not the column default.
    """

    def setUp(self):
        self.migration = _load_migration_module()

    def _create_pre_migration_component(self, limit_period_value=""):
        """Create an OfferingComponent that mimics the pre-0226 empty state.

        Migration 0172 set ``null=True, blank=True`` on the column; 0226
        tightens it back to NOT NULL via AlterField. After all migrations
        have applied, only ``""`` is reachable for the "needs backfill"
        state — the migration treats NULL and "" as equivalent so this
        suffices to exercise the RunPython logic.
        """
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
        )
        # Bypass OfferingComponent.save()'s "" → "month" coercion via UPDATE.
        type(component).objects.filter(pk=component.pk).update(
            limit_period=limit_period_value
        )
        component.refresh_from_db()
        return offering, component

    def test_quarterly_policy_backfills_component_to_quarterly(self):
        """policy.period=MONTH_3 + component.limit_period="" → "quarterly"."""
        offering, component = self._create_pre_migration_component()
        SlurmPeriodicUsagePolicyFactory(
            scope=offering,
            period=PeriodMixin.Periods.MONTH_3,
        )

        self.migration.backfill_limit_period(django_apps, schema_editor=None)

        component.refresh_from_db()
        self.assertEqual(
            component.limit_period,
            LimitPeriods.QUARTERLY,
            "Component on a quarterly policy must be backfilled to 'quarterly', "
            "not silently flipped to 'month'.",
        )

    def test_annual_policy_backfills_component_to_annual(self):
        """policy.period=MONTH_12 + component.limit_period="" → "annual"."""
        offering, component = self._create_pre_migration_component()
        SlurmPeriodicUsagePolicyFactory(
            scope=offering,
            period=PeriodMixin.Periods.MONTH_12,
        )

        self.migration.backfill_limit_period(django_apps, schema_editor=None)

        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.ANNUAL)

    def test_monthly_policy_backfills_component_to_month(self):
        """policy.period=MONTH_1 (default) + component.limit_period=NULL → "month"."""
        offering, component = self._create_pre_migration_component()
        SlurmPeriodicUsagePolicyFactory(
            scope=offering,
            period=PeriodMixin.Periods.MONTH_1,
        )

        self.migration.backfill_limit_period(django_apps, schema_editor=None)

        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.MONTH)

    def test_no_policy_backfills_component_to_month(self):
        """Components without an associated SLURM policy keep the historical default."""
        _, component = self._create_pre_migration_component()

        self.migration.backfill_limit_period(django_apps, schema_editor=None)

        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.MONTH)

    def test_does_not_overwrite_explicit_component_value(self):
        """A component already set by an operator must not be touched."""
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.ANNUAL,
        )
        SlurmPeriodicUsagePolicyFactory(
            scope=offering,
            period=PeriodMixin.Periods.MONTH_3,  # disagrees on purpose
        )

        self.migration.backfill_limit_period(django_apps, schema_editor=None)

        component.refresh_from_db()
        self.assertEqual(
            component.limit_period,
            LimitPeriods.ANNUAL,
            "Migration must not rewrite component values that were already set.",
        )

    def test_idempotent_on_already_migrated_state(self):
        """Re-running the backfill against a fully-migrated database is a no-op.

        This is the state of any deployment that already ran the broken
        version of 0226 (every limit_period already "month"). The fix must
        not regress that idempotency.
        """
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        SlurmPeriodicUsagePolicyFactory(
            scope=offering,
            period=PeriodMixin.Periods.MONTH_3,
        )

        self.migration.backfill_limit_period(django_apps, schema_editor=None)

        component.refresh_from_db()
        # Idempotency: an already-migrated component is left alone (the fact
        # that it's wrong is exactly what the separate data-repair migration
        # has to address — this migration must not.)
        self.assertEqual(component.limit_period, LimitPeriods.MONTH)
