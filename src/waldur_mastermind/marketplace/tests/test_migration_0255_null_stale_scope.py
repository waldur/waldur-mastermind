"""Coverage for marketplace migration 0255_null_stale_scope_content_types.

Offerings created before their scope model was dropped from the codebase
(e.g. support.Offering, removed in WAL-3743) keep content_type_id/object_id
pointing at a content type whose model_class() is None. The migration must
null those pointers and leave healthy scopes untouched.
"""

import importlib.util
import pathlib

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "0255_null_stale_scope_content_types.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "_migration_0255_under_test", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Migration0255NullStaleScopeTest(TestCase):
    def setUp(self):
        self.migration = _load_migration_module()
        self.stale_ct, _ = ContentType.objects.get_or_create(
            app_label="support", model="offering"
        )

    def _make_stale(self, instance):
        type(instance).objects.filter(pk=instance.pk).update(
            content_type=self.stale_ct, object_id=12345
        )
        instance.refresh_from_db()

    def test_offering_with_stale_content_type_is_nulled(self):
        offering = marketplace_factories.OfferingFactory()
        self._make_stale(offering)

        self.migration.null_stale_scopes(django_apps, schema_editor=None)

        offering.refresh_from_db()
        self.assertIsNone(offering.content_type)
        self.assertIsNone(offering.object_id)
        self.assertIsNone(offering.scope)

    def test_resource_with_stale_content_type_is_nulled(self):
        resource = marketplace_factories.ResourceFactory()
        self._make_stale(resource)

        self.migration.null_stale_scopes(django_apps, schema_editor=None)

        resource.refresh_from_db()
        self.assertIsNone(resource.content_type)
        self.assertIsNone(resource.object_id)

    def test_offering_with_valid_scope_is_untouched(self):
        settings = structure_factories.ServiceSettingsFactory()
        offering = marketplace_factories.OfferingFactory(scope=settings)

        self.migration.null_stale_scopes(django_apps, schema_editor=None)

        offering.refresh_from_db()
        self.assertEqual(offering.scope, settings)

    def test_offering_without_scope_is_untouched(self):
        offering = marketplace_factories.OfferingFactory()
        self.assertIsNone(offering.content_type_id)

        self.migration.null_stale_scopes(django_apps, schema_editor=None)

        offering.refresh_from_db()
        self.assertIsNone(offering.content_type)
        self.assertIsNone(models.Offering.objects.get(pk=offering.pk).scope)
