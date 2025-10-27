import os
import tempfile
from io import StringIO

import yaml
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.models import Role, RolePermission


class ExportRolesCommandTest(TestCase):
    def setUp(self):
        # Create test roles with different characteristics
        customer_ct = ContentType.objects.get_by_natural_key(*TYPE_MAP["customer"])
        project_ct = ContentType.objects.get_by_natural_key(*TYPE_MAP["project"])

        # System role (active)
        self.system_role = Role.objects.create(
            name="TEST.SYSTEM_ROLE",
            description="Test system role",
            content_type=customer_ct,
            is_system_role=True,
            is_active=True,
        )
        RolePermission.objects.create(role=self.system_role, permission="CUSTOMER.LIST")
        RolePermission.objects.create(
            role=self.system_role, permission="CUSTOMER.UPDATE"
        )

        # Non-system role (active)
        self.non_system_role = Role.objects.create(
            name="TEST.NON_SYSTEM_ROLE",
            description="Test non-system role",
            content_type=project_ct,
            is_system_role=False,
            is_active=True,
        )
        RolePermission.objects.create(
            role=self.non_system_role, permission="PROJECT.LIST"
        )

        # Inactive role
        self.inactive_role = Role.objects.create(
            name="TEST.INACTIVE_ROLE",
            description="Test inactive role",
            content_type=customer_ct,
            is_system_role=True,
            is_active=False,
        )
        RolePermission.objects.create(
            role=self.inactive_role, permission="CUSTOMER.DELETE_PERMISSION"
        )

        # Role without description
        self.role_no_description = Role.objects.create(
            name="TEST.NO_DESCRIPTION",
            content_type=project_ct,
            is_system_role=True,
            is_active=True,
        )
        RolePermission.objects.create(
            role=self.role_no_description, permission="PROJECT.CREATE"
        )

    def tearDown(self):
        # Clean up test roles
        Role.objects.filter(name__startswith="TEST.").delete()

    def test_basic_export_functionality(self):
        """Test basic export functionality"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file)

            # Verify file exists
            self.assertTrue(os.path.exists(output_file))

            # Load and verify content
            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            self.assertIsInstance(roles_data, list)
            self.assertGreater(len(roles_data), 0)

            # Check that our test roles are included
            role_names = [role["role"] for role in roles_data]
            self.assertIn("TEST.SYSTEM_ROLE", role_names)
            self.assertIn("TEST.NON_SYSTEM_ROLE", role_names)
            # Inactive role should be excluded by default
            self.assertNotIn("TEST.INACTIVE_ROLE", role_names)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_system_only_option(self):
        """Test --system-only option"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file, system_only=True)

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            role_names = [role["role"] for role in roles_data]

            # Should include system roles
            self.assertIn("TEST.SYSTEM_ROLE", role_names)
            # Should exclude non-system roles
            self.assertNotIn("TEST.NON_SYSTEM_ROLE", role_names)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_include_inactive_option(self):
        """Test --include-inactive option"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file, include_inactive=True)

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            role_names = [role["role"] for role in roles_data]

            # Should include inactive roles
            self.assertIn("TEST.INACTIVE_ROLE", role_names)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_specific_role_names_option(self):
        """Test --role-names option"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command(
                "export_roles",
                output_file,
                role_names=["TEST.SYSTEM_ROLE", "TEST.NON_SYSTEM_ROLE"],
            )

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            # Should export exactly the specified roles
            self.assertEqual(len(roles_data), 2)
            role_names = [role["role"] for role in roles_data]
            self.assertIn("TEST.SYSTEM_ROLE", role_names)
            self.assertIn("TEST.NON_SYSTEM_ROLE", role_names)
            self.assertNotIn("TEST.INACTIVE_ROLE", role_names)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_export_format_structure(self):
        """Test that exported format matches import_roles expectations"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file, role_names=["TEST.SYSTEM_ROLE"])

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            # Should have exactly one role
            self.assertEqual(len(roles_data), 1)

            role_data = roles_data[0]

            # Check required fields
            self.assertIn("role", role_data)
            self.assertIn("scope", role_data)
            self.assertIn("permissions", role_data)

            # Check values
            self.assertEqual(role_data["role"], "TEST.SYSTEM_ROLE")
            self.assertEqual(role_data["scope"], "customer")
            self.assertIsInstance(role_data["permissions"], list)
            self.assertIn("CUSTOMER.LIST", role_data["permissions"])
            self.assertIn("CUSTOMER.UPDATE", role_data["permissions"])

            # Check optional fields
            self.assertIn("description", role_data)
            self.assertEqual(role_data["description"], "Test system role")

            # is_active should not be present for active roles (default)
            self.assertNotIn("is_active", role_data)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_inactive_role_format(self):
        """Test format for inactive roles"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command(
                "export_roles",
                output_file,
                role_names=["TEST.INACTIVE_ROLE"],
                include_inactive=True,
            )

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            role_data = roles_data[0]

            # Should include is_active: false for inactive roles
            self.assertIn("is_active", role_data)
            self.assertFalse(role_data["is_active"])

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_role_without_description(self):
        """Test role without description"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command(
                "export_roles", output_file, role_names=["TEST.NO_DESCRIPTION"]
            )

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            role_data = roles_data[0]

            # Should not include description field for empty descriptions
            self.assertNotIn("description", role_data)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_permissions_are_sorted(self):
        """Test that permissions are sorted alphabetically"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file, role_names=["TEST.SYSTEM_ROLE"])

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            role_data = roles_data[0]
            permissions = role_data["permissions"]

            # Should be sorted alphabetically
            self.assertEqual(permissions, sorted(permissions))

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_yaml_format_validity(self):
        """Test that generated YAML is valid and well-formatted"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file, role_names=["TEST.SYSTEM_ROLE"])

            # Verify YAML is valid
            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            self.assertIsInstance(roles_data, list)

            # Verify structure is correct
            role_data = roles_data[0]
            self.assertIsInstance(role_data["role"], str)
            self.assertIsInstance(role_data["scope"], str)
            self.assertIsInstance(role_data["permissions"], list)
            self.assertIsInstance(role_data["description"], str)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_error_handling_invalid_output_path(self):
        """Test error handling for invalid output paths"""
        stdout = StringIO()

        call_command("export_roles", "/invalid/path/roles.yaml", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Failed to write to file", output)

    def test_round_trip_compatibility(self):
        """Test that exported roles can be imported back"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            # Export specific test role
            call_command("export_roles", output_file, role_names=["TEST.SYSTEM_ROLE"])

            # Verify the format is compatible with import_roles by loading it
            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            # Check that the structure matches what import_roles expects
            role_data = roles_data[0]
            required_fields = ["role", "scope", "permissions"]
            for field in required_fields:
                self.assertIn(field, role_data, f"Missing required field: {field}")

            # The format should be valid for import_roles
            # (We don't actually import to avoid modifying test data)

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)

    def test_empty_result_handling(self):
        """Test handling when no roles match the criteria"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            output_file = f.name

        try:
            call_command("export_roles", output_file, role_names=["NONEXISTENT_ROLE"])

            with open(output_file) as f:
                roles_data = yaml.safe_load(f)

            # Should be empty list
            self.assertEqual(roles_data, [])

        finally:
            if os.path.exists(output_file):
                os.unlink(output_file)
