import tempfile

import yaml
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role, RolePermission


def run_import_roles(rows):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as f:
        yaml.safe_dump(rows, f)
        f.flush()
        call_command("import_roles", f.name)


class ImportRolesCommandTest(TestCase):
    def test_creates_a_new_role_with_permissions(self):
        run_import_roles(
            [
                {
                    "role": "TEST.NEW_ROLE",
                    "scope": "customer",
                    "permissions": ["CUSTOMER.LIST_USERS"],
                }
            ]
        )

        role = Role.objects.get(name="TEST.NEW_ROLE")
        self.assertTrue(role.is_system_role)
        self.assertEqual(
            {p.permission for p in RolePermission.objects.filter(role=role)},
            {"CUSTOMER.LIST_USERS"},
        )

    def test_replaces_the_full_permission_set_of_an_existing_role(self):
        customer_ct = ContentType.objects.get_by_natural_key(*TYPE_MAP["customer"])
        role = Role.objects.create(
            name="TEST.EXISTING_ROLE", content_type=customer_ct, is_system_role=True
        )
        RolePermission.objects.create(role=role, permission="CUSTOMER.LIST_USERS")

        run_import_roles(
            [
                {
                    "role": "TEST.EXISTING_ROLE",
                    "scope": "customer",
                    "permissions": ["PROJECT.LIST"],
                }
            ]
        )

        self.assertEqual(
            {p.permission for p in RolePermission.objects.filter(role=role)},
            {"PROJECT.LIST"},
        )

    def test_is_active_false_deactivates_the_role(self):
        customer_ct = ContentType.objects.get_by_natural_key(*TYPE_MAP["customer"])
        role = Role.objects.create(
            name="TEST.ACTIVE_ROLE",
            content_type=customer_ct,
            is_system_role=True,
            is_active=True,
        )

        run_import_roles(
            [
                {
                    "role": "TEST.ACTIVE_ROLE",
                    "scope": "customer",
                    "permissions": [],
                    "is_active": False,
                }
            ]
        )

        role.refresh_from_db()
        self.assertFalse(role.is_active)

    def test_is_active_true_leaves_an_active_role_active(self):
        customer_ct = ContentType.objects.get_by_natural_key(*TYPE_MAP["customer"])
        role = Role.objects.create(
            name="TEST.ACTIVE_ROLE",
            content_type=customer_ct,
            is_system_role=True,
            is_active=True,
        )

        run_import_roles(
            [
                {
                    "role": "TEST.ACTIVE_ROLE",
                    "scope": "customer",
                    "permissions": [],
                    "is_active": True,
                }
            ]
        )

        role.refresh_from_db()
        self.assertTrue(role.is_active)
