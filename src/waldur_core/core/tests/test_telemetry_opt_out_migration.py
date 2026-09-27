import importlib

from django.apps import apps
from django.test import TestCase

from waldur_core.core.models import Feature

migration = importlib.import_module(
    "waldur_core.core.migrations.0051_telemetry_opt_out"
)


class TelemetryOptOutMigrationTest(TestCase):
    def _run(self):
        migration.carry_over_legacy_telemetry_opt_out(apps, None)

    def _value(self, key):
        return Feature.objects.filter(key=key).values_list("value", flat=True).first()

    def test_nothing_is_written_without_legacy_row(self):
        self._run()

        self.assertFalse(Feature.objects.filter(key=migration.CURRENT_KEY).exists())

    def test_legacy_opt_out_is_carried_over(self):
        Feature.objects.create(key=migration.LEGACY_KEY, value=False)

        self._run()

        self.assertFalse(self._value(migration.CURRENT_KEY))
        self.assertFalse(Feature.objects.filter(key=migration.LEGACY_KEY).exists())

    def test_legacy_opt_in_is_dropped(self):
        Feature.objects.create(key=migration.LEGACY_KEY, value=True)

        self._run()

        self.assertFalse(Feature.objects.filter(key=migration.CURRENT_KEY).exists())
        self.assertFalse(Feature.objects.filter(key=migration.LEGACY_KEY).exists())

    def test_current_key_wins_over_legacy_value(self):
        Feature.objects.create(key=migration.LEGACY_KEY, value=False)
        Feature.objects.create(key=migration.CURRENT_KEY, value=True)

        self._run()

        self.assertTrue(self._value(migration.CURRENT_KEY))
        self.assertFalse(Feature.objects.filter(key=migration.LEGACY_KEY).exists())
