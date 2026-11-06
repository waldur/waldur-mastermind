import importlib

from django.apps import apps
from django.test import TestCase

from waldur_core.configuration.models import Setting
from waldur_core.server.constance_settings import REPORTING_SCREEN_CHOICES

migration = importlib.import_module(
    "waldur_core.configuration.migrations.0004_drop_removed_reporting_screens"
)


class RemovedReportingScreensTest(TestCase):
    def test_a_stored_value_forgets_the_removed_screens(self):
        Setting.objects.create(
            key=migration.KEY,
            value=["resource-usage", "call-performance", "review-progress", "growth"],
            source="api",
        )
        migration.drop_removed_screens(apps, None)
        self.assertEqual(
            Setting.objects.get(key=migration.KEY).value, ["resource-usage", "growth"]
        )

    def test_the_removed_screens_are_no_longer_offered(self):
        offered = {key for key, _label in REPORTING_SCREEN_CHOICES}
        self.assertFalse(offered & migration.REMOVED)
