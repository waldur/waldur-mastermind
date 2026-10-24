import importlib

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings
from reversion.models import Revision, Version

from waldur_mastermind.marketplace.tests import factories as marketplace_factories

migration = importlib.import_module(
    "waldur_mastermind.google.migrations.0005_delete_models"
)
LOGGER = "waldur_mastermind.google.migrations.0005_delete_models"


class FakeSchemaEditor:
    connection = connection


def apps_before_deletion():
    """Every app at its latest state, except google just before 0005."""
    # The test database may be built with migrations disabled; read them anyway.
    with override_settings(MIGRATION_MODULES={}):
        loader = MigrationLoader(None)
    nodes = [node for node in loader.graph.leaf_nodes() if node[0] != "google"]
    nodes.append(("google", "0001_squashed_0004"))
    return loader.project_state(nodes).apps


class DeleteModelsMigrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.apps = apps_before_deletion()

    def setUp(self):
        self.GoogleCalendar = self.apps.get_model("google", "GoogleCalendar")
        self.GoogleCredentials = self.apps.get_model("google", "GoogleCredentials")
        with connection.schema_editor() as editor:
            editor.create_model(self.GoogleCalendar)
            editor.create_model(self.GoogleCredentials)

    def report(self):
        migration.report_google_data(self.apps, FakeSchemaEditor())

    def test_quiet_without_google_data(self):
        with self.assertNoLogs(LOGGER):
            self.report()

    def test_quiet_for_calendars_never_created_in_google(self):
        for backend_id in (None, ""):
            self.GoogleCalendar.objects.create(
                offering_id=marketplace_factories.OfferingFactory().pk,
                backend_id=backend_id,
            )

        with self.assertNoLogs(LOGGER):
            self.report()

    def test_lists_each_calendar_and_asks_to_unshare(self):
        public = marketplace_factories.OfferingFactory(name="Microscope")
        private = marketplace_factories.OfferingFactory(name="Telescope")
        self.GoogleCalendar.objects.create(
            offering_id=public.pk, backend_id="public@group", public=True
        )
        self.GoogleCalendar.objects.create(
            offering_id=private.pk, backend_id="private@group", public=False
        )

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.report()

        self.assertEqual(len(logs.output), 3)
        self.assertIn("Microscope", logs.output[0])
        self.assertIn(public.uuid.hex, logs.output[0])
        self.assertIn(public.customer.name, logs.output[0])
        self.assertIn(public.customer.uuid.hex, logs.output[0])
        self.assertIn("backend_id=public@group, public=True", logs.output[0])
        self.assertIn("Telescope", logs.output[1])
        self.assertIn("backend_id=private@group, public=False", logs.output[1])
        self.assertIn("Unshare or delete", logs.output[2])

    def test_lists_providers_with_credentials_without_tokens(self):
        provider = marketplace_factories.ServiceProviderFactory()
        self.GoogleCredentials.objects.create(
            service_provider_id=provider.pk,
            calendar_token="access-secret",
            calendar_refresh_token="refresh-secret",
        )

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.report()

        self.assertEqual(len(logs.output), 2)
        self.assertIn(provider.uuid.hex, logs.output[0])
        self.assertIn(provider.customer.name, logs.output[0])
        self.assertIn("revoke Waldur's access", logs.output[1])
        for line in logs.output:
            self.assertNotIn("secret", line)

    def test_deletes_versions_of_google_models_only(self):
        offering = marketplace_factories.OfferingFactory()
        revision = Revision.objects.create(date_created=offering.created)
        calendar_type, _ = ContentType.objects.get_or_create(
            app_label="google", model="googlecalendar"
        )
        credentials_type, _ = ContentType.objects.get_or_create(
            app_label="google", model="googlecredentials"
        )
        offering_type = ContentType.objects.get_for_model(offering)
        versions = {
            content_type: Version.objects.create(
                revision=revision,
                object_id=str(offering.pk),
                content_type=content_type,
                db="default",
                format="json",
                serialized_data="[]",
                object_repr="",
            )
            for content_type in (calendar_type, credentials_type, offering_type)
        }

        migration.delete_versions(self.apps, FakeSchemaEditor())

        self.assertFalse(
            Version.objects.filter(
                content_type__in=[calendar_type, credentials_type]
            ).exists()
        )
        self.assertTrue(Version.objects.filter(pk=versions[offering_type].pk).exists())
