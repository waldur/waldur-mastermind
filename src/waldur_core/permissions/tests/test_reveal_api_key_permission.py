from importlib import import_module
from pathlib import Path

import yaml
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure.models import Customer, Project

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"
PERMISSION = PermissionEnum.REVEAL_RESOURCE_API_KEY.value
HOLDERS = {
    "CUSTOMER.OWNER",
    "PROJECT.ADMIN",
    "PROJECT.MANAGER",
    "PROJECT.MEMBER",
    "PROJECT.PRINCIPAL_INVESTIGATOR",
    "PROJECT.CO_INVESTIGATOR",
}

migration = import_module(
    "waldur_core.permissions.migrations.0039_reveal_api_key_permission"
)


class RevealApiKeyPermissionGrantTest(test.APITestCase):
    def test_permissions_yaml_grants_it_to_the_owner_and_project_working_roles(
        self,
    ):
        holders = {
            row["role"]
            for row in yaml.safe_load(PERMISSIONS_YAML.read_text())
            if PERMISSION in row["permissions"]
        }
        self.assertEqual(holders, HOLDERS)


class RevealApiKeyPermissionMigrationTest(test.APITestCase):
    def system_role(self, name, model, template=None):
        role, _ = Role.objects.get_or_create(
            name=name,
            content_type=ContentType.objects.get_for_model(model),
            defaults={"is_system_role": True},
        )
        if template:
            role.template = template
            role.save(update_fields=["template"])
        return role

    def test_default_roles_and_their_clones_get_it(self):
        owner = self.system_role("CUSTOMER.OWNER", Customer)
        support = self.system_role("CUSTOMER.SUPPORT", Customer)
        reader = self.system_role("CUSTOMER.READER", Customer)
        admin = self.system_role("PROJECT.ADMIN", Project)
        manager = self.system_role("PROJECT.MANAGER", Project)
        member = self.system_role("PROJECT.MEMBER", Project)
        investigator = self.system_role(
            "PROJECT.PRINCIPAL_INVESTIGATOR", Project, template=manager
        )
        co_investigator = self.system_role(
            "PROJECT.CO_INVESTIGATOR", Project, template=admin
        )
        # A system role templated on a holder is not its clone.
        collaborator = self.system_role(
            "PROJECT.COLLABORATOR", Project, template=member
        )
        clones = [
            Role.objects.create(
                name=f"{template.name.split('.')[0]}.acme.{template.name.split('.')[-1]}",
                content_type=template.content_type,
                template=template,
            )
            for template in (owner, admin, manager, member)
        ]
        support_clone = Role.objects.create(
            name="CUSTOMER.acme.SUPPORT",
            content_type=support.content_type,
            template=support,
        )
        unrelated = Role.objects.create(
            name="CUSTOMER.acme.OTHER", content_type=owner.content_type
        )
        RolePermission.objects.filter(permission=PERMISSION).delete()

        migration.grant_reveal_api_key(apps, None)
        migration.grant_reveal_api_key(apps, None)  # idempotent

        holders = list(
            RolePermission.objects.filter(permission=PERMISSION).values_list(
                "role__name", flat=True
            )
        )
        self.assertEqual(
            sorted(holders),
            sorted(
                [
                    owner.name,
                    admin.name,
                    manager.name,
                    member.name,
                    investigator.name,
                    co_investigator.name,
                ]
                + [role.name for role in clones]
            ),
        )
        for role in (support, reader, collaborator, support_clone, unrelated):
            self.assertNotIn(role.name, holders)
