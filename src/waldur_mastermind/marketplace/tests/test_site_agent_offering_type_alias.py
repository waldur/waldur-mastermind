"""The site-agent type is now Marketplace.SiteAgent; Marketplace.Slurm stays an alias.

The legacy name is accepted on every input path and resolved to the canonical
one, while responses, the plugin list and stored rows only carry the new name.
"""

import importlib
import tempfile
from io import StringIO

import yaml
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.http import QueryDict
from django.test import TestCase

from waldur_core.configuration import config
from waldur_core.configuration.models import Setting
from waldur_core.configuration.testing import override_config
from waldur_core.core.serializers import ConstanceSettingsSerializer
from waldur_mastermind.marketplace import filters, models, plugins
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    LEGACY_SITE_AGENT_OFFERING,
    SITE_AGENT_OFFERING,
    expand_offering_types,
    normalize_offering_type,
)
from waldur_mastermind.marketplace.serializers import OfferingCreateSerializer
from waldur_mastermind.marketplace.tests import factories

migration = importlib.import_module(
    "waldur_mastermind.marketplace.migrations.0322_rename_site_agent_offering_type"
)


class OfferingTypeHelpersTest(TestCase):
    def test_legacy_name_resolves_to_canonical(self):
        self.assertEqual(
            normalize_offering_type(LEGACY_SITE_AGENT_OFFERING), SITE_AGENT_OFFERING
        )
        self.assertEqual(normalize_offering_type(BASIC_OFFERING), BASIC_OFFERING)

    def test_either_name_expands_to_both(self):
        both = sorted([SITE_AGENT_OFFERING, LEGACY_SITE_AGENT_OFFERING])
        self.assertEqual(expand_offering_types([LEGACY_SITE_AGENT_OFFERING]), both)
        self.assertEqual(expand_offering_types([SITE_AGENT_OFFERING]), both)
        self.assertEqual(expand_offering_types([BASIC_OFFERING]), [BASIC_OFFERING])


class PluginManagerAliasTest(TestCase):
    def test_legacy_name_reaches_site_agent_processors(self):
        for processor_type in (
            "create_resource_processor",
            "update_resource_processor",
            "delete_resource_processor",
        ):
            self.assertIs(
                plugins.manager.get_processor(
                    LEGACY_SITE_AGENT_OFFERING, processor_type
                ),
                plugins.manager.get_processor(SITE_AGENT_OFFERING, processor_type),
            )

    def test_type_is_listed_once_under_its_canonical_name(self):
        types = plugins.manager.get_offering_types()
        self.assertIn(SITE_AGENT_OFFERING, types)
        self.assertNotIn(LEGACY_SITE_AGENT_OFFERING, types)

    def test_disabling_the_legacy_name_disables_the_type(self):
        with override_config(DISABLED_OFFERING_TYPES=[LEGACY_SITE_AGENT_OFFERING]):
            self.assertNotIn(SITE_AGENT_OFFERING, plugins.manager.get_offering_types())

    def test_registry_swapped_for_a_plain_dict_keeps_the_alias(self):
        original = plugins.manager.backends
        try:
            plugins.manager.backends = dict(original)
            self.assertIn(LEGACY_SITE_AGENT_OFFERING, plugins.manager.backends)
        finally:
            plugins.manager.backends = original


class OfferingCreateTypeTest(TestCase):
    def test_legacy_name_is_accepted_and_stored_as_canonical(self):
        self.assertEqual(
            OfferingCreateSerializer().validate_type(LEGACY_SITE_AGENT_OFFERING),
            SITE_AGENT_OFFERING,
        )


def query(**params):
    """Filter input as a real query string arrives, not a plain dict."""
    data = QueryDict(mutable=True)
    data.update(params)
    return data


class OfferingTypeFilterTest(TestCase):
    def setUp(self):
        self.site_agent = factories.OfferingFactory(type=SITE_AGENT_OFFERING)
        # A row a raw SQL write or bulk_create left under the old name.
        self.legacy = factories.OfferingFactory(type=LEGACY_SITE_AGENT_OFFERING)
        self.basic = factories.OfferingFactory(type=BASIC_OFFERING)

    def filter_offerings(self, offering_type):
        return set(
            filters.OfferingFilter(
                data=query(type=offering_type), queryset=models.Offering.objects.all()
            ).qs
        )

    def test_both_names_return_the_same_offerings(self):
        expected = {self.site_agent, self.legacy}
        self.assertEqual(self.filter_offerings(LEGACY_SITE_AGENT_OFFERING), expected)
        self.assertEqual(self.filter_offerings(SITE_AGENT_OFFERING), expected)

    def test_other_types_are_unaffected(self):
        self.assertEqual(self.filter_offerings(BASIC_OFFERING), {self.basic})

    def test_order_offering_type_filter_accepts_legacy_name(self):
        order = factories.OrderFactory(offering=self.site_agent)
        factories.OrderFactory(offering=self.basic)
        result = filters.OrderFilter(
            data=query(offering_type=LEGACY_SITE_AGENT_OFFERING),
            queryset=models.Order.objects.all(),
        ).qs
        self.assertEqual(set(result), {order})

    def test_resource_offering_type_filter_accepts_legacy_name(self):
        resource = factories.ResourceFactory(offering=self.site_agent)
        factories.ResourceFactory(offering=self.basic)
        result = filters.ResourceFilter(
            data=query(offering_type=LEGACY_SITE_AGENT_OFFERING),
            queryset=models.Resource.objects.all(),
        ).qs
        self.assertEqual(set(result), {resource})

    def test_component_usage_offering_type_filter_accepts_legacy_name(self):
        usage = factories.ComponentUsageMonthlyFactory(
            component=factories.OfferingComponentFactory(offering=self.site_agent)
        )
        factories.ComponentUsageMonthlyFactory(
            component=factories.OfferingComponentFactory(offering=self.basic)
        )
        result = filters.ComponentUsageMonthlyFilter(
            data=query(offering_type=LEGACY_SITE_AGENT_OFFERING),
            queryset=models.ComponentUsageMonthly.objects.all(),
        ).qs
        self.assertEqual(set(result), {usage})


class DisabledOfferingTypesInputTest(TestCase):
    """The setting is an input path too: deployments may still use the old name."""

    def apply_deployment_file(self, value):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as settings_file:
            yaml.safe_dump({"DISABLED_OFFERING_TYPES": value}, settings_file)
            settings_file.flush()
            call_command(
                "override_constance_settings", settings_file.name, stdout=StringIO()
            )

    def test_deployment_file_with_the_legacy_name_is_applied(self):
        self.apply_deployment_file([BASIC_OFFERING, LEGACY_SITE_AGENT_OFFERING])

        self.assertEqual(
            config.DISABLED_OFFERING_TYPES,
            [BASIC_OFFERING, LEGACY_SITE_AGENT_OFFERING],
        )
        offering_types = plugins.manager.get_offering_types()
        self.assertNotIn(BASIC_OFFERING, offering_types)
        self.assertNotIn(SITE_AGENT_OFFERING, offering_types)

    def test_settings_api_accepts_either_name(self):
        for name in (LEGACY_SITE_AGENT_OFFERING, SITE_AGENT_OFFERING):
            serializer = ConstanceSettingsSerializer(
                data={"DISABLED_OFFERING_TYPES": [name]}
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)


class RenameSiteAgentOfferingTypeMigrationTest(TestCase):
    def _set_disabled(self, value):
        Setting.objects.update_or_create(
            key=migration.DISABLED_TYPES_KEY,
            defaults={"value": value, "source": "api"},
        )

    def _disabled_row(self):
        return Setting.objects.get(key=migration.DISABLED_TYPES_KEY)

    def _get_disabled(self):
        return self._disabled_row().value

    def _run(self, direction):
        with connection.schema_editor() as schema_editor:
            direction(apps, schema_editor)

    def test_offerings_and_disabled_types_are_renamed_and_restored(self):
        offering = factories.OfferingFactory(type=migration.OLD_TYPE)
        basic = factories.OfferingFactory(type=BASIC_OFFERING)
        self._set_disabled([BASIC_OFFERING, migration.OLD_TYPE])

        self._run(migration.forwards)
        offering.refresh_from_db()
        basic.refresh_from_db()
        self.assertEqual(offering.type, migration.NEW_TYPE)
        self.assertEqual(basic.type, BASIC_OFFERING)
        self.assertFalse(models.Offering.objects.filter(type=migration.OLD_TYPE))
        self.assertEqual(self._get_disabled(), [BASIC_OFFERING, migration.NEW_TYPE])

        self._run(migration.backwards)
        offering.refresh_from_db()
        self.assertEqual(offering.type, migration.OLD_TYPE)
        self.assertEqual(self._get_disabled(), [BASIC_OFFERING, migration.OLD_TYPE])

    def test_both_names_in_disabled_types_collapse_to_one(self):
        self._set_disabled([migration.OLD_TYPE, migration.NEW_TYPE])
        self._run(migration.forwards)
        self.assertEqual(self._get_disabled(), [migration.NEW_TYPE])

    def test_rewriting_disabled_types_records_the_migration_as_writer(self):
        self._set_disabled([migration.OLD_TYPE])
        self._run(migration.forwards)
        row = self._disabled_row()
        self.assertEqual(row.source, "migration")
        self.assertIsNone(row.updated_by)

    def test_disabled_types_without_the_old_name_are_left_alone(self):
        self._set_disabled([BASIC_OFFERING])
        self._run(migration.forwards)
        row = self._disabled_row()
        self.assertEqual(row.value, [BASIC_OFFERING])
        self.assertEqual(row.source, "api")

    def test_missing_disabled_types_setting_is_left_alone(self):
        Setting.objects.filter(key=migration.DISABLED_TYPES_KEY).delete()
        self._run(migration.forwards)
        self.assertFalse(
            Setting.objects.filter(key=migration.DISABLED_TYPES_KEY).exists()
        )
