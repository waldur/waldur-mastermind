import importlib

from django.apps import apps as global_apps
from django.test import TestCase

from waldur_core.core.enums import ReviewStates
from waldur_mastermind.marketplace.tests import factories

migration = importlib.import_module(
    "waldur_mastermind.marketplace.migrations.0291_enable_limit_change_requests_where_used"
)
OPTION = migration.OPTION


def run_migration():
    migration.enable_limit_change_requests_where_used(global_apps, None)


class EnableLimitChangeRequestsWhereUsedTest(TestCase):
    def test_offering_with_requests_is_opted_in(self):
        request = factories.ResourceLimitChangeRequestFactory(
            state=ReviewStates.APPROVED
        )
        offering = request.resource.offering
        run_migration()
        offering.refresh_from_db()
        self.assertIs(offering.plugin_options[OPTION], True)

    def test_child_offering_opts_in_its_parent(self):
        parent = factories.OfferingFactory()
        child = factories.OfferingFactory(parent=parent)
        factories.ResourceLimitChangeRequestFactory(
            resource=factories.ResourceFactory(offering=child)
        )
        run_migration()
        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertIs(parent.plugin_options[OPTION], True)
        self.assertNotIn(OPTION, child.plugin_options)

    def test_offering_without_requests_stays_off(self):
        offering = factories.OfferingFactory()
        run_migration()
        offering.refresh_from_db()
        self.assertNotIn(OPTION, offering.plugin_options)

    def test_explicit_choice_is_kept(self):
        request = factories.ResourceLimitChangeRequestFactory()
        offering = request.resource.offering
        offering.plugin_options = {OPTION: False}
        offering.save()
        run_migration()
        offering.refresh_from_db()
        self.assertIs(offering.plugin_options[OPTION], False)

    def test_other_options_are_preserved(self):
        request = factories.ResourceLimitChangeRequestFactory()
        offering = request.resource.offering
        offering.plugin_options = {"auto_approve_remote_orders": True}
        offering.save()
        run_migration()
        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options,
            {"auto_approve_remote_orders": True, OPTION: True},
        )
