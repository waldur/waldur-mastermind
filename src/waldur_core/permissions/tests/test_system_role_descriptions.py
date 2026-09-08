from pathlib import Path

import yaml
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions.enums import ROLE_DESCRIPTIONS, TYPE_MAP, RoleEnum
from waldur_core.permissions.models import Role, RoleManager
from waldur_core.permissions.serializers import RoleDetailsSerializer

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"


def permissions_yaml_rows():
    """The deployed role definitions, or None outside a source checkout."""
    if not PERMISSIONS_YAML.exists():
        return None
    return yaml.safe_load(PERMISSIONS_YAML.read_text())


def content_type_for(role_name, rows):
    """The content type a role is actually scoped to, per permissions.yaml."""
    scope = next(row["scope"] for row in rows if row["role"] == role_name)
    return ContentType.objects.get_by_natural_key(*TYPE_MAP[scope])


class SystemRoleDescriptionTest(TestCase):
    def setUp(self):
        RoleManager.clear_cache()
        self.rows = permissions_yaml_rows()
        if self.rows is None:
            self.skipTest(f"{PERMISSIONS_YAML} not available")

    def tearDown(self):
        RoleManager.clear_cache()

    def test_lazily_created_role_gets_a_description(self):
        # CALL.PANEL_MEMBER has no seeding migration, so it is only ever created
        # on first use. Without a default it lands blank and the UI falls back
        # to rendering the raw enum name.
        content_type = ContentType.objects.get_by_natural_key("proposal", "call")
        Role.objects.filter(name=RoleEnum.CALL_PANEL_MEMBER).delete()

        role = Role.objects.get_system_role(
            RoleEnum.CALL_PANEL_MEMBER, content_type=content_type
        )

        self.assertEqual(role.description, "Call panel member")

    def test_every_lazily_created_system_role_gets_a_description(self):
        for role_name, expected in ROLE_DESCRIPTIONS.items():
            with self.subTest(role=role_name):
                # Scope each role the way it is really scoped; under
                # --no-migrations none of them are seeded, so there is no
                # existing row to take a content type from.
                content_type = content_type_for(role_name, self.rows)
                Role.objects.filter(name=role_name).delete()
                RoleManager.clear_cache()

                role = Role.objects.get_system_role(
                    role_name, content_type=content_type
                )

                self.assertEqual(role.description, expected)
                self.assertEqual(role.content_type, content_type)

    def test_description_is_served_by_the_api(self):
        # description is a modeltranslation field: the API serves
        # description_<language>, so writing only the raw column is not enough.
        content_type = ContentType.objects.get_by_natural_key("proposal", "call")
        Role.objects.filter(name=RoleEnum.CALL_PANEL_MEMBER).delete()

        role = Role.objects.get_system_role(
            RoleEnum.CALL_PANEL_MEMBER, content_type=content_type
        )

        self.assertEqual(role.description_en, "Call panel member")
        self.assertEqual(
            RoleDetailsSerializer(role).data["description"], "Call panel member"
        )

    def test_existing_description_is_not_overwritten(self):
        content_type = ContentType.objects.get_by_natural_key("structure", "customer")
        Role.objects.filter(name=RoleEnum.CUSTOMER_READER).delete()
        Role.objects.create(
            name=RoleEnum.CUSTOMER_READER,
            description="Locally renamed",
            content_type=content_type,
            is_system_role=True,
        )

        role = Role.objects.get_system_role(
            RoleEnum.CUSTOMER_READER, content_type=content_type
        )

        self.assertEqual(role.description, "Locally renamed")

    def test_descriptions_match_permissions_yaml(self):
        # import_roles rewrites descriptions from permissions.yaml on every
        # deployment, so a drifting mapping would be silently reverted.
        yaml_descriptions = {row["role"]: row.get("description") for row in self.rows}

        for role_name, description in ROLE_DESCRIPTIONS.items():
            with self.subTest(role=role_name):
                self.assertIn(role_name, yaml_descriptions)
                self.assertEqual(description, yaml_descriptions[role_name])

    def test_every_role_in_permissions_yaml_has_a_description(self):
        # The other direction: a role added to the deployed configuration
        # without an entry here would be created blank and never repaired,
        # which is the bug this mapping exists to prevent.
        for role_name in (row["role"] for row in self.rows):
            with self.subTest(role=role_name):
                self.assertIn(role_name, ROLE_DESCRIPTIONS)
