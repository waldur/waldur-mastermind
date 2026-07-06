"""Tests for the 0247 enable_posix_account backfill.

The migration function is exercised directly against the live app registry
(django_test_migrations is not available), mirroring test_posix_pool_migration.
"""

from django.apps import apps as live_apps
from rest_framework import test

from waldur_mastermind.marketplace.migrations._enable_posix_account_backfill import (
    backfill_enable_posix_account,
)
from waldur_mastermind.marketplace.tests import factories

# Offerings only qualify for the backfill when they manage user accounts.
MANAGES_ACCOUNTS = {"service_provider_can_create_offering_user": True}


class EnablePosixAccountBackfillTest(test.APITestCase):
    def test_sets_true_when_missing(self):
        offering = factories.OfferingFactory(
            plugin_options={**MANAGES_ACCOUNTS, "homedir_prefix": "/home/"}
        )
        backfill_enable_posix_account(live_apps, None)
        offering.refresh_from_db()
        self.assertIs(offering.plugin_options["enable_posix_account"], True)
        # other keys are preserved
        self.assertEqual(offering.plugin_options["homedir_prefix"], "/home/")

    def test_preserves_explicit_false(self):
        offering = factories.OfferingFactory(
            plugin_options={**MANAGES_ACCOUNTS, "enable_posix_account": False}
        )
        backfill_enable_posix_account(live_apps, None)
        offering.refresh_from_db()
        self.assertIs(offering.plugin_options["enable_posix_account"], False)

    def test_stamps_minimal_account_managing_offering(self):
        offering = factories.OfferingFactory(plugin_options=dict(MANAGES_ACCOUNTS))
        backfill_enable_posix_account(live_apps, None)
        offering.refresh_from_db()
        self.assertIs(offering.plugin_options["enable_posix_account"], True)

    def test_skips_offering_that_does_not_manage_accounts(self):
        # Without service_provider_can_create_offering_user the offering never
        # creates POSIX accounts, so the key is left untouched.
        offering = factories.OfferingFactory(
            plugin_options={"homedir_prefix": "/home/"}
        )
        backfill_enable_posix_account(live_apps, None)
        offering.refresh_from_db()
        self.assertNotIn("enable_posix_account", offering.plugin_options)
