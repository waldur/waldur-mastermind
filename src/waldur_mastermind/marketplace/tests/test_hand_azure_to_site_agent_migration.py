"""Tests for the migrations that hand Azure VMs to the site agent.

0287 converts Azure.VirtualMachine offerings to Site Agent offerings and gives
each resource its full Azure resource ID; 0288 drops the removed app's tables,
its service settings and its content types.
"""

from importlib import import_module
from types import SimpleNamespace

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BASIC_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

# The module names start with a digit, so they cannot be imported by name.
handover = import_module(
    "waldur_mastermind.marketplace.migrations.0287_hand_azure_vms_to_site_agent"
)
cleanup = import_module(
    "waldur_mastermind.marketplace.migrations.0288_drop_azure_tables"
)

SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
RG_ARM_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/created-rg"
CREATED_VM_ARM_ID = f"{RG_ARM_ID}/providers/Microsoft.Compute/virtualMachines/created"

# Only the columns 0287 reads; the real tables have many more.
CREATE_AZURE_TABLES = """
CREATE TABLE waldur_azure_resourcegroup (
    id integer PRIMARY KEY, name varchar(150), backend_id varchar(255)
);
CREATE TABLE waldur_azure_virtualmachine (
    id integer PRIMARY KEY, name varchar(150), backend_id varchar(255),
    resource_group_id integer, service_settings_id integer
);
"""


# pytest may run with --no-migrations, which empties MIGRATION_MODULES and with
# it the loader; the historical registry is built from the migration files.
@override_settings(MIGRATION_MODULES={})
class HandAzureToSiteAgentMigrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.historical_apps = MigrationLoader(None).project_state().apps

    def setUp(self):
        self.schema_editor = SimpleNamespace(connection=connection)

        self.azure_settings = structure_factories.ServiceSettingsFactory()
        self.other_settings = structure_factories.ServiceSettingsFactory()
        # update(): the live registry no longer has a backend for "Azure", so
        # save handlers must not see the type.
        structure_models.ServiceSettings.objects.filter(
            pk=self.azure_settings.pk
        ).update(
            type=cleanup.AZURE_SERVICE_TYPE,
            options={"subscription_id": SUBSCRIPTION, "tenant_id": "tenant"},
        )

        self.vm_offering = marketplace_factories.OfferingFactory(
            type=handover.VM_OFFERING_TYPE,
            scope=self.azure_settings,
            plugin_options={"supports_pausing": False, "keep_me": "yes"},
        )
        self.sql_offering = marketplace_factories.OfferingFactory(
            type=handover.SQL_SERVER_OFFERING_TYPE
        )
        self.unrelated_offering = marketplace_factories.OfferingFactory(
            type=BASIC_OFFERING,
            scope=self.other_settings,
            plugin_options={"keep_me": "yes"},
        )

        self.order = marketplace_factories.OrderFactory(offering=self.vm_offering)
        self.created = self.order.resource
        self.invoice_item = invoices_factories.InvoiceItemFactory(resource=self.created)
        self.imported = marketplace_factories.ResourceFactory(offering=self.vm_offering)
        self.imported_in_arm_rg = marketplace_factories.ResourceFactory(
            offering=self.vm_offering
        )

    def _create_azure_rows(self):
        with connection.cursor() as cursor:
            cursor.execute(CREATE_AZURE_TABLES)
            cursor.execute(
                "INSERT INTO waldur_azure_resourcegroup VALUES "
                "(1, 'created-rg', %s), (2, 'imported-rg', 'imported-rg')",
                [RG_ARM_ID],
            )
            cursor.execute(
                "INSERT INTO waldur_azure_virtualmachine VALUES "
                "(10, 'created', %s, 1, %s), "
                "(11, 'imported', 'imported', 2, %s), "
                "(12, 'imported-2', 'imported-2', 1, %s)",
                [CREATED_VM_ARM_ID] + [self.azure_settings.pk] * 3,
            )
        self.vm_ct = ContentType.objects.create(
            app_label="waldur_azure", model="virtualmachine"
        )
        # update(): saving would fire handlers that try to resolve the scope,
        # and the live registry has no model for it.
        for resource, vm_id, backend_id in (
            (self.created, 10, CREATED_VM_ARM_ID),
            (self.imported, 11, "imported"),
            (self.imported_in_arm_rg, 12, "imported-2"),
        ):
            marketplace_models.Resource.objects.filter(pk=resource.pk).update(
                content_type=self.vm_ct, object_id=vm_id, backend_id=backend_id
            )

    def _run(self):
        handover.hand_azure_vms_to_site_agent(self.historical_apps, self.schema_editor)
        with connection.cursor() as cursor:
            cursor.execute(cleanup.DROP_TABLES)
        cleanup.delete_azure_service_settings(self.historical_apps, self.schema_editor)
        cleanup.drop_azure_content_types(self.historical_apps, self.schema_editor)

    def _backend_id(self, resource):
        resource.refresh_from_db()
        return resource.backend_id

    def test_vm_offering_is_handed_to_the_site_agent(self):
        self._create_azure_rows()
        self._run()

        self.vm_offering.refresh_from_db()
        self.assertEqual(self.vm_offering.type, handover.SITE_AGENT_OFFERING_TYPE)
        self.assertEqual(
            self.vm_offering.plugin_options,
            {"supports_pausing": True, "supports_downscaling": True, "keep_me": "yes"},
        )
        self.assertIsNone(self.vm_offering.content_type_id)
        self.assertIsNone(self.vm_offering.object_id)

    def test_resources_get_full_azure_resource_ids(self):
        self._create_azure_rows()
        self._run()

        self.assertEqual(self._backend_id(self.created), CREATED_VM_ARM_ID)
        self.assertEqual(
            self._backend_id(self.imported),
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/imported-rg"
            "/providers/Microsoft.Compute/virtualMachines/imported",
        )
        self.assertEqual(
            self._backend_id(self.imported_in_arm_rg),
            f"{RG_ARM_ID}/providers/Microsoft.Compute/virtualMachines/imported-2",
        )
        self.created.refresh_from_db()
        self.assertIsNone(self.created.content_type_id)
        self.assertIsNone(self.created.object_id)

    def test_resource_order_and_invoice_item_are_kept(self):
        state = self.order.state
        self._create_azure_rows()
        self._run()

        self.created.refresh_from_db()
        self.assertEqual(self.created.offering_id, self.vm_offering.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, state)
        self.assertEqual(self.order.resource_id, self.created.pk)
        self.invoice_item.refresh_from_db()
        self.assertEqual(self.invoice_item.resource_id, self.created.pk)

    def test_sql_server_offering_is_deleted(self):
        self._run()

        self.assertFalse(
            marketplace_models.Offering.objects.filter(pk=self.sql_offering.pk).exists()
        )

    def test_unrelated_offering_is_untouched(self):
        self._create_azure_rows()
        self._run()

        self.unrelated_offering.refresh_from_db()
        self.assertEqual(self.unrelated_offering.type, BASIC_OFFERING)
        self.assertEqual(self.unrelated_offering.plugin_options, {"keep_me": "yes"})
        self.assertEqual(self.unrelated_offering.scope, self.other_settings)

    def test_only_azure_service_settings_are_deleted(self):
        settings_count = structure_models.ServiceSettings.objects.count()
        invoice_items = invoices_models.InvoiceItem.objects.count()
        self._create_azure_rows()
        self._run()

        self.assertFalse(
            structure_models.ServiceSettings.objects.filter(
                pk=self.azure_settings.pk
            ).exists()
        )
        self.assertEqual(
            structure_models.ServiceSettings.objects.count(), settings_count - 1
        )
        self.assertEqual(invoices_models.InvoiceItem.objects.count(), invoice_items)
        self.assertFalse(ContentType.objects.filter(app_label="waldur_azure").exists())

    def test_without_azure_tables_backend_ids_are_left_alone(self):
        # Fresh installs never had the tables; nothing can be resolved, so
        # nothing is guessed.
        marketplace_models.Resource.objects.filter(pk=self.imported.pk).update(
            backend_id="imported"
        )
        self._run()

        self.assertEqual(self._backend_id(self.imported), "imported")
        self.vm_offering.refresh_from_db()
        self.assertEqual(self.vm_offering.type, handover.SITE_AGENT_OFFERING_TYPE)

    def test_without_azure_data_nothing_changes(self):
        marketplace_models.Offering.objects.filter(
            type__in=[handover.VM_OFFERING_TYPE, handover.SQL_SERVER_OFFERING_TYPE]
        ).delete()
        structure_models.ServiceSettings.objects.filter(
            pk=self.azure_settings.pk
        ).delete()
        offerings = set(marketplace_models.Offering.objects.values_list("pk", "type"))
        settings_count = structure_models.ServiceSettings.objects.count()

        self._run()

        self.assertEqual(
            set(marketplace_models.Offering.objects.values_list("pk", "type")),
            offerings,
        )
        self.assertEqual(
            structure_models.ServiceSettings.objects.count(), settings_count
        )
