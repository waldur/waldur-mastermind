from importlib import import_module
from pathlib import Path

import yaml
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure.models import Customer

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"
PERMISSION = PermissionEnum.LIST_INVOICES.value
HOLDERS = {"CUSTOMER.OWNER", "CUSTOMER.SUPPORT", "CUSTOMER.READER"}

migration = import_module(
    "waldur_core.permissions.migrations.0038_invoice_list_permission"
)


class InvoiceListPermissionGrantTest(test.APITestCase):
    def test_permissions_yaml_grants_it_to_every_role_that_read_invoices(self):
        holders = {
            row["role"]
            for row in yaml.safe_load(PERMISSIONS_YAML.read_text())
            if PERMISSION in row["permissions"]
        }
        self.assertEqual(holders, HOLDERS)


class InvoiceListPermissionMigrationTest(test.APITestCase):
    def system_role(self, name, model):
        role, _ = Role.objects.get_or_create(
            name=name,
            content_type=ContentType.objects.get_for_model(model),
            defaults={"is_system_role": True},
        )
        return role

    def test_default_roles_and_their_clones_get_it(self):
        owner = self.system_role("CUSTOMER.OWNER", Customer)
        support = self.system_role("CUSTOMER.SUPPORT", Customer)
        reader = self.system_role("CUSTOMER.READER", Customer)
        clones = [
            Role.objects.create(
                name=f"CUSTOMER.acme.{template.name.split('.')[-1]}",
                content_type=template.content_type,
                template=template,
            )
            for template in (owner, support, reader)
        ]
        unrelated = Role.objects.create(
            name="CUSTOMER.acme.OTHER", content_type=owner.content_type
        )
        RolePermission.objects.filter(permission=PERMISSION).delete()

        migration.grant_invoice_list(apps, None)
        migration.grant_invoice_list(apps, None)  # idempotent

        holders = list(
            RolePermission.objects.filter(permission=PERMISSION).values_list(
                "role__name", flat=True
            )
        )
        self.assertEqual(
            sorted(holders),
            sorted(
                [owner.name, support.name, reader.name] + [role.name for role in clones]
            ),
        )
        self.assertNotIn(unrelated.name, holders)
