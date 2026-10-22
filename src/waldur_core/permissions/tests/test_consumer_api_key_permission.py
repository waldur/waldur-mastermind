from importlib import import_module
from pathlib import Path

import yaml
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests.factories import UserFactory

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"
NEW = PermissionEnum.MANAGE_RESOURCE_CONSUMER_API_KEY.value
OLD = PermissionEnum.MANAGE_RESOURCE_USERS.value

migration = import_module(
    "waldur_core.permissions.migrations.0035_consumer_api_key_permission"
)


class ConsumerApiKeyPermissionTest(test.APITestCase):
    def test_permissions_yaml_grants_it_to_the_owner_and_project_roles(self):
        roles = {
            row["role"]: row["permissions"]
            for row in yaml.safe_load(PERMISSIONS_YAML.read_text())
        }
        for name in ("CUSTOMER.OWNER", "PROJECT.ADMIN", "PROJECT.MANAGER"):
            self.assertIn(NEW, roles[name], name)
        holders = {name for name, permissions in roles.items() if NEW in permissions}
        self.assertEqual(
            holders, {"CUSTOMER.OWNER", "PROJECT.ADMIN", "PROJECT.MANAGER"}
        )


class ConsumerApiKeyMigrationTest(test.APITestCase):
    def system_role(self, name, model):
        role, _ = Role.objects.get_or_create(
            name=name,
            content_type=ContentType.objects.get_for_model(model),
            defaults={"is_system_role": True},
        )
        return role

    def test_holders_of_the_old_permission_and_project_roles_get_it(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        owner = self.system_role("CUSTOMER.OWNER", Customer)
        admin = self.system_role("PROJECT.ADMIN", Project)
        manager = self.system_role("PROJECT.MANAGER", Project)
        member = self.system_role("PROJECT.MEMBER", Project)
        project_ct = ContentType.objects.get_for_model(Project)
        clones = [
            Role.objects.create(
                name="CUSTOMER.acme.OWNER", content_type=customer_ct, template=owner
            ),
            Role.objects.create(
                name="PROJECT.acme.ADMIN", content_type=project_ct, template=admin
            ),
            Role.objects.create(
                name="PROJECT.acme.MANAGER", content_type=project_ct, template=manager
            ),
        ]
        custom = Role.objects.create(
            name="CUSTOMER.acme.KEYS", content_type=customer_ct
        )
        unrelated = Role.objects.create(
            name="CUSTOMER.acme.OTHER", content_type=customer_ct
        )
        RolePermission.objects.filter(permission=NEW).delete()
        RolePermission.objects.filter(permission=OLD).delete()
        for role in (owner, custom):
            RolePermission.objects.create(role=role, permission=OLD)

        migration.add_consumer_api_key_permission(apps, None)
        migration.add_consumer_api_key_permission(apps, None)  # idempotent

        holders = set(
            RolePermission.objects.filter(permission=NEW).values_list(
                "role__name", flat=True
            )
        )
        self.assertEqual(
            holders,
            {
                owner.name,
                admin.name,
                manager.name,
                custom.name,
                *(clone.name for clone in clones),
            },
        )
        self.assertNotIn(member.name, holders)
        self.assertNotIn(unrelated.name, holders)
        self.assertEqual(
            RolePermission.objects.filter(role=custom, permission=NEW).count(), 1
        )
        # The old permission stays where it was.
        self.assertTrue(
            RolePermission.objects.filter(role=custom, permission=OLD).exists()
        )

    def test_tokens_scoped_to_the_old_permission_get_the_new_one(self):
        user = UserFactory()
        scoped, _ = create_pat(user, name="keys", scopes=[OLD, "RESOURCE.LIST"])
        already, _ = create_pat(user, name="both", scopes=[OLD, NEW])
        other, _ = create_pat(user, name="other", scopes=["RESOURCE.LIST"])

        migration.add_consumer_api_key_permission(apps, None)
        migration.add_consumer_api_key_permission(apps, None)  # idempotent

        for token in (scoped, already, other):
            token.refresh_from_db()
        self.assertEqual(scoped.scopes, [OLD, "RESOURCE.LIST", NEW])
        self.assertEqual(already.scopes, [OLD, NEW])
        self.assertEqual(other.scopes, ["RESOURCE.LIST"])
