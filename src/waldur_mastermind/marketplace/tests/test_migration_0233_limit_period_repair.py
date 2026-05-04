"""Coverage for marketplace migration 0233_repair_limit_period_after_0226.

The repair must rewrite "month" → "quarterly" / "annual" / "total" on LIMIT
components of offerings whose SlurmPeriodicUsagePolicy declares a non-monthly
period (the cohort damaged by the original 0226 backfill — see WAL-9907) and
must not touch anything else.
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
    / "0233_repair_limit_period_after_0226.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "_migration_0233_under_test", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Migration0233RepairTest(TestCase):
    def setUp(self):
        self.migration = _load_migration_module()

    def _create_corrupted_state(self, policy_period):
        """Reproduce the post-0226 damage on a quarterly/annual offering:
        component.limit_period="month" but policy.period is non-monthly."""
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        SlurmPeriodicUsagePolicyFactory(scope=offering, period=policy_period)
        return offering, component

    def test_repairs_quarterly_policy_to_quarterly_component(self):
        _, component = self._create_corrupted_state(PeriodMixin.Periods.MONTH_3)
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.QUARTERLY)

    def test_repairs_annual_policy_to_annual_component(self):
        _, component = self._create_corrupted_state(PeriodMixin.Periods.MONTH_12)
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.ANNUAL)

    def test_repairs_total_policy_to_total_component(self):
        _, component = self._create_corrupted_state(PeriodMixin.Periods.TOTAL)
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.TOTAL)

    def test_leaves_monthly_policy_alone(self):
        """Offerings with a monthly policy were always meant to be monthly —
        the repair must not touch them."""
        _, component = self._create_corrupted_state(PeriodMixin.Periods.MONTH_1)
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.MONTH)

    def test_leaves_offerings_without_policy_alone(self):
        """A "month" component on an offering without a SLURM policy was either
        always monthly or out of scope for this fix."""
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.MONTH)

    def test_does_not_overwrite_explicit_non_month_value(self):
        """If an operator already corrected a component to a non-month value,
        the repair must not rewrite it (filter is restricted to "month")."""
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.QUARTERLY,
        )
        SlurmPeriodicUsagePolicyFactory(
            scope=offering, period=PeriodMixin.Periods.MONTH_12
        )
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.QUARTERLY)

    def test_idempotent(self):
        """Running the repair twice must not change the result."""
        _, component = self._create_corrupted_state(PeriodMixin.Periods.MONTH_3)
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        component.refresh_from_db()
        self.assertEqual(component.limit_period, LimitPeriods.QUARTERLY)

    def test_only_repairs_limit_components(self):
        """USAGE/FIXED components must not be touched even on a quarterly offering."""
        offering = marketplace_factories.OfferingFactory()
        usage_component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.USAGE,
            limit_period=LimitPeriods.MONTH,
        )
        SlurmPeriodicUsagePolicyFactory(
            scope=offering, period=PeriodMixin.Periods.MONTH_3
        )
        self.migration.repair_limit_period(django_apps, schema_editor=None)
        usage_component.refresh_from_db()
        self.assertEqual(usage_component.limit_period, LimitPeriods.MONTH)
