import io
import tempfile
from pathlib import Path

import yaml
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, RolePermission
from waldur_core.structure.models import Customer

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"
PERMISSION = PermissionEnum.MANAGE_OFFERING_AGENT.value
LIST_KEYS = PermissionEnum.LIST_SERVICE_PROVIDER_KEYS.value


class ManageOfferingAgentPermissionGrantTest(test.APITestCase):
    def test_permissions_yaml_grants_it_to_owners_and_offering_managers(self):
        # Service provider managers hold their role on the ServiceProvider,
        # which the agent's resource actions do not accept, so they cannot run
        # an agent today and are not given the permission.
        holders = {
            row["role"]
            for row in yaml.safe_load(PERMISSIONS_YAML.read_text())
            if PERMISSION in row["permissions"]
        }
        self.assertEqual(holders, {"CUSTOMER.OWNER", "OFFERING.MANAGER"})

    def test_offering_managers_get_the_ssh_keys_of_their_offering_users(self):
        # Owners hold it on the organization already; service provider
        # managers hold it on the provider.
        holders = {
            row["role"]
            for row in yaml.safe_load(PERMISSIONS_YAML.read_text())
            if LIST_KEYS in row["permissions"]
        }
        self.assertEqual(
            holders, {"CUSTOMER.OWNER", "CUSTOMER.MANAGER", "OFFERING.MANAGER"}
        )


class OverrideDropsManageAgentTest(test.APITestCase):
    """A drop list written before OFFERING.MANAGE_AGENT existed keeps its
    effect: dropping offering management drops agent management with it."""

    def setUp(self):
        self.owner, _ = Role.objects.get_or_create(
            name="CUSTOMER.OWNER",
            content_type=ContentType.objects.get_for_model(Customer),
            defaults={"is_system_role": True},
        )
        for permission in ("OFFERING.CREATE", PERMISSION):
            RolePermission.objects.get_or_create(role=self.owner, permission=permission)

    def override(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as override_file:
            override_file.write(text)
            override_file.flush()
            call_command("override_roles", override_file.name, stdout=io.StringIO())
        return set(self.owner.permissions.values_list("permission", flat=True))

    def test_dropping_offering_create_drops_manage_agent(self):
        held = self.override(
            "- role: CUSTOMER.OWNER\n  drop_permissions:\n    - OFFERING.CREATE\n"
        )
        self.assertNotIn(PERMISSION, held)

    def test_listing_it_under_add_permissions_keeps_it(self):
        held = self.override(
            "- role: CUSTOMER.OWNER\n"
            "  add_permissions:\n    - OFFERING.MANAGE_AGENT\n"
            "  drop_permissions:\n    - OFFERING.CREATE\n"
        )
        self.assertIn(PERMISSION, held)

    def test_unrelated_drop_keeps_it(self):
        held = self.override(
            "- role: CUSTOMER.OWNER\n  drop_permissions:\n    - OFFERING.DELETE\n"
        )
        self.assertIn(PERMISSION, held)

    def test_kept_while_the_role_holds_another_broader_permission(self):
        RolePermission.objects.get_or_create(
            role=self.owner, permission="OFFERING.UPDATE"
        )
        held = self.override(
            "- role: CUSTOMER.OWNER\n  drop_permissions:\n    - OFFERING.CREATE\n"
        )
        self.assertIn(PERMISSION, held)

        held = self.override(
            "- role: CUSTOMER.OWNER\n  drop_permissions:\n    - OFFERING.UPDATE\n"
        )
        self.assertNotIn(PERMISSION, held)
