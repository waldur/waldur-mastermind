import importlib

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APITestCase

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.permissions.tests.test_system_role_descriptions import (
    permissions_yaml_rows,
)
from waldur_core.structure.models import Customer


class ShippedRoomPermissionTest(APITestCase):
    def test_owner_role_carries_create_matrix_room(self):
        # Tests never load permissions.yaml, so the fixture grants it by hand;
        # this pins the shipped role.
        rows = permissions_yaml_rows()
        if rows is None:
            self.skipTest("permissions.yaml is not part of this checkout")
        owner = next(row for row in rows if row["role"] == "CUSTOMER.OWNER")
        self.assertIn(PermissionEnum.CREATE_MATRIX_ROOM.value, owner["permissions"])


class OwnerCreateMatrixRoomMigrationTest(APITestCase):
    migration = importlib.import_module(
        "waldur_core.permissions.migrations.0032_owner_create_matrix_room"
    )

    def test_owner_clones_receive_the_permission(self):
        clone = Role.objects.create(
            name="CUSTOMER.acme.OWNER",
            content_type=ContentType.objects.get_for_model(Customer),
            template=CustomerRole.OWNER,
        )
        self.migration.add_create_matrix_room_to_owners(apps, None)
        self.assertTrue(
            RolePermission.objects.filter(
                role=clone, permission=PermissionEnum.CREATE_MATRIX_ROOM
            ).exists()
        )

    def test_other_customer_roles_are_left_alone(self):
        self.migration.add_create_matrix_room_to_owners(apps, None)
        self.assertFalse(
            RolePermission.objects.filter(
                role=CustomerRole.SUPPORT,
                permission=PermissionEnum.CREATE_MATRIX_ROOM,
            ).exists()
        )
