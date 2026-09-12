"""Tests for migration 0027_delete_sql_server.

The Azure.SQLServer offering is gone, so its offerings are deleted with
everything that cascades from them, and generic pointers at the two SQL
content types are cleared before those content types are removed.
"""

from importlib import import_module

from django.contrib.contenttypes.models import ContentType
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_azure import VIRTUAL_MACHINE_TYPE
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories

# The module name starts with a digit, so it cannot be imported by name.
migration = import_module("waldur_azure.migrations.0027_delete_sql_server")


# pytest may run with --no-migrations, which empties MIGRATION_MODULES and with
# it the loader; the historical registry is built from the migration files.
@override_settings(MIGRATION_MODULES={})
class DeleteSqlServerMigrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical_apps = MigrationLoader(None).project_state().apps

    def setUp(self):
        # The live registry no longer knows the SQL models, so their content
        # types exist only as leftovers in upgraded databases.
        self.sql_server_ct = ContentType.objects.create(
            app_label="waldur_azure", model="sqlserver"
        )
        self.sql_offering = marketplace_factories.OfferingFactory(
            type=migration.SQL_SERVER_OFFERING_TYPE
        )
        self.order = marketplace_factories.OrderFactory(offering=self.sql_offering)
        self.resource = self.order.resource
        self.invoice_item = invoices_factories.InvoiceItemFactory(
            resource=self.resource
        )
        self.issue = support_factories.IssueFactory()
        # Point at the SQL server with update(): saving would fire handlers that
        # try to resolve the scope, and the live registry has no model for it.
        marketplace_models.Resource.objects.filter(pk=self.resource.pk).update(
            content_type=self.sql_server_ct, object_id=1
        )
        support_models.Issue.objects.filter(pk=self.issue.pk).update(
            resource_content_type=self.sql_server_ct, resource_object_id=1
        )
        self.vm_offering = marketplace_factories.OfferingFactory(
            type=VIRTUAL_MACHINE_TYPE
        )

    def _run(self):
        migration.delete_sql_server_data(self.historical_apps, None)
        migration.delete_sql_content_types(self.historical_apps, None)

    def test_sql_offering_and_its_resources_and_orders_are_deleted(self):
        self._run()

        self.assertFalse(
            marketplace_models.Offering.objects.filter(pk=self.sql_offering.pk).exists()
        )
        self.assertFalse(
            marketplace_models.Resource.objects.filter(pk=self.resource.pk).exists()
        )
        self.assertFalse(
            marketplace_models.Order.objects.filter(pk=self.order.pk).exists()
        )

    def test_invoice_item_survives_without_resource(self):
        self._run()

        self.invoice_item.refresh_from_db()
        self.assertIsNone(self.invoice_item.resource_id)

    def test_support_issue_survives_without_resource_scope(self):
        self._run()

        self.issue.refresh_from_db()
        self.assertIsNone(self.issue.resource_content_type_id)
        self.assertIsNone(self.issue.resource_object_id)

    def test_offering_roles_are_revoked(self):
        user = structure_factories.UserFactory()
        self.sql_offering.add_user(user, OfferingRole.MANAGER)

        self._run()

        role = UserRole.objects.get(user=user, object_id=self.sql_offering.pk)
        self.assertFalse(role.is_active)

    def test_vm_offering_and_content_types_are_handled(self):
        self._run()

        self.assertTrue(
            marketplace_models.Offering.objects.filter(pk=self.vm_offering.pk).exists()
        )
        self.assertFalse(
            ContentType.objects.filter(
                app_label="waldur_azure", model__in=migration.SQL_MODELS
            ).exists()
        )
