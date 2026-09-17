import tempfile
from importlib import import_module
from io import StringIO

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from rest_framework import test

from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace.models import Offering
from waldur_sram.models import SramGroup

migration = import_module(
    "waldur_core.permissions.migrations.0030_view_team_for_existing_roles"
)


def permissions_of(role):
    return set(role.permissions.values_list("permission", flat=True))


class ExistingRolesMigrationTest(test.APITestCase):
    def run_migration(self):
        with connection.schema_editor() as schema_editor:
            migration.add_view_team_to_existing_roles(apps, schema_editor)

    def test_every_existing_organization_and_project_role_keeps_team_view(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        project_ct = ContentType.objects.get_for_model(Project)
        auditor = Role.objects.create(name="CUSTOMER.AUDITOR", content_type=customer_ct)
        guest = Role.objects.create(name="PROJECT.GUEST", content_type=project_ct)
        offering_role = Role.objects.create(
            name="OFFERING.OBSERVER",
            content_type=ContentType.objects.get_for_model(Offering),
        )
        already = Role.objects.create(name="CUSTOMER.HAS_IT", content_type=customer_ct)
        already.add_permission("CUSTOMER.VIEW_TEAM")

        self.run_migration()
        self.run_migration()  # idempotent

        self.assertIn("CUSTOMER.VIEW_TEAM", permissions_of(auditor))
        self.assertNotIn("PROJECT.VIEW_TEAM", permissions_of(auditor))
        self.assertIn("PROJECT.VIEW_TEAM", permissions_of(guest))
        self.assertEqual(permissions_of(offering_role), set())
        self.assertEqual(
            RolePermission.objects.filter(
                role=already, permission="CUSTOMER.VIEW_TEAM"
            ).count(),
            1,
        )

    def test_sram_placeholder_roles_stay_private(self):
        customer = Customer.objects.create(name="uuc", backend_id="uuc")
        placeholder = Role.objects.create(
            name="CUSTOMER.uuc.SRAM.research",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        SramGroup.objects.create(
            external_id="co@sram",
            display_name="Research",
            urn="uuc:research",
            kind="co",
            customer=customer,
            role=placeholder,
        )

        self.run_migration()

        self.assertEqual(permissions_of(placeholder), set())


class ImportRolesWarningTest(test.APITestCase):
    def import_roles(self, content):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as roles_file:
            roles_file.write(content)
            roles_file.flush()
            out = StringIO()
            call_command("import_roles", roles_file.name, stdout=out)
        return out.getvalue()

    def test_warns_when_an_organization_role_lacks_team_view(self):
        output = self.import_roles(
            "- role: CUSTOMER.AUDITOR\n"
            "  scope: customer\n"
            "  permissions:\n"
            "    - CUSTOMER.LIST_USERS\n"
        )
        self.assertIn("CUSTOMER.AUDITOR does not grant CUSTOMER.VIEW_TEAM", output)

    def test_no_warning_when_granted_or_not_team_scoped(self):
        output = self.import_roles(
            "- role: CUSTOMER.AUDITOR\n"
            "  scope: customer\n"
            "  permissions:\n"
            "    - CUSTOMER.VIEW_TEAM\n"
            "- role: OFFERING.OBSERVER\n"
            "  scope: offering\n"
            "  permissions: []\n"
        )
        self.assertNotIn("does not grant", output)
