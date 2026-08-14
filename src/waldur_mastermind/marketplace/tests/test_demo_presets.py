import collections
import decimal
import json
import pathlib
from io import StringIO

from ddt import data, ddt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.demo_presets.manifest import (
    DemoPresetManager,
    PresetMetadata,
)


class DemoPresetManagerTest(TestCase):
    """Unit tests for DemoPresetManager."""

    def test_list_presets_returns_available_presets(self):
        """Test that list_presets returns metadata for all JSON files."""
        presets = DemoPresetManager.list_presets()

        self.assertIsInstance(presets, list)
        # Should have at least our 4 predefined presets
        self.assertGreaterEqual(len(presets), 4)

        preset_names = [p.name for p in presets]
        self.assertIn("minimal_quickstart", preset_names)
        self.assertIn("hpc_ai_platform", preset_names)
        self.assertIn("government_cloud", preset_names)
        self.assertIn("research_institution", preset_names)

    def test_list_presets_returns_preset_metadata_objects(self):
        """Test that list_presets returns PresetMetadata objects."""
        presets = DemoPresetManager.list_presets()

        for preset in presets:
            self.assertIsInstance(preset, PresetMetadata)
            self.assertTrue(hasattr(preset, "name"))
            self.assertTrue(hasattr(preset, "title"))
            self.assertTrue(hasattr(preset, "description"))
            self.assertTrue(hasattr(preset, "version"))
            self.assertTrue(hasattr(preset, "entity_counts"))
            self.assertTrue(hasattr(preset, "scenarios"))

    def test_get_preset_info_returns_metadata(self):
        """Test that get_preset_info returns correct metadata."""
        preset = DemoPresetManager.get_preset_info("minimal_quickstart")

        self.assertIsNotNone(preset)
        self.assertEqual(preset.name, "minimal_quickstart")
        self.assertIsInstance(preset.title, str)
        self.assertIsInstance(preset.description, str)
        self.assertIsInstance(preset.entity_counts, dict)
        self.assertIsInstance(preset.scenarios, list)

    def test_get_preset_info_returns_none_for_invalid_name(self):
        """Test that get_preset_info returns None for non-existent preset."""
        preset = DemoPresetManager.get_preset_info("nonexistent_preset")
        self.assertIsNone(preset)

    def test_get_preset_path_returns_path_for_valid_preset(self):
        """Test that get_preset_path returns path for existing preset."""
        path = DemoPresetManager.get_preset_path("minimal_quickstart")

        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertTrue(path.name.endswith(".json"))

    def test_get_preset_path_returns_none_for_invalid_preset(self):
        """Test that get_preset_path returns None for non-existent preset."""
        path = DemoPresetManager.get_preset_path("nonexistent_preset")
        self.assertIsNone(path)

    def test_minimal_quickstart_preset_has_expected_entities(self):
        """Test that minimal_quickstart preset has expected entity counts."""
        preset = DemoPresetManager.get_preset_info("minimal_quickstart")

        self.assertIsNotNone(preset)
        self.assertGreater(preset.entity_counts.get("users", 0), 0)
        self.assertGreater(preset.entity_counts.get("customers", 0), 0)
        self.assertGreater(preset.entity_counts.get("projects", 0), 0)

    def test_hpc_ai_preset_has_expected_entities(self):
        """Test that hpc_ai_platform preset has expected entity counts."""
        preset = DemoPresetManager.get_preset_info("hpc_ai_platform")

        self.assertIsNotNone(preset)
        self.assertGreater(preset.entity_counts.get("users", 0), 0)
        self.assertGreater(preset.entity_counts.get("offerings", 0), 0)

    def test_government_cloud_preset_has_expected_entities(self):
        """Test that government_cloud preset has expected entity counts."""
        preset = DemoPresetManager.get_preset_info("government_cloud")

        self.assertIsNotNone(preset)
        self.assertGreater(preset.entity_counts.get("customers", 0), 0)
        self.assertGreater(preset.entity_counts.get("categories", 0), 0)

    def test_research_institution_preset_has_expected_entities(self):
        """Test that research_institution preset has expected entity counts."""
        preset = DemoPresetManager.get_preset_info("research_institution")

        self.assertIsNotNone(preset)
        self.assertGreater(preset.entity_counts.get("projects", 0), 0)
        self.assertGreater(preset.entity_counts.get("service_providers", 0), 0)

    def test_call_management_credits_fit_inside_their_organization_grant(self):
        """Every project allocation must fit inside its organization's grant.

        The credit history generator draws project allocations against the
        organization credit, so a project funded above its organization would
        produce a history that cannot happen in production.
        """
        path = DemoPresetManager.get_preset_path("call_management")
        data = json.loads(pathlib.Path(path).read_text())

        customer_credits = {
            credit["customer_uuid"]: decimal.Decimal(credit["value"])
            for credit in data["customer_credits"]
        }
        customer_by_project = {
            project["uuid"]: project["customer_uuid"] for project in data["projects"]
        }
        self.assertTrue(customer_credits)
        self.assertTrue(data["project_credits"])

        granted_per_customer = collections.defaultdict(decimal.Decimal)
        for credit in data["project_credits"]:
            customer_uuid = customer_by_project[credit["project_uuid"]]
            granted_per_customer[customer_uuid] += decimal.Decimal(credit["value"])

        for customer_uuid, granted in granted_per_customer.items():
            self.assertIn(customer_uuid, customer_credits)
            self.assertLessEqual(granted, customer_credits[customer_uuid])

    def test_load_preset_returns_error_for_invalid_name(self):
        """Test that load_preset returns error for non-existent preset."""
        result = DemoPresetManager.load_preset("nonexistent_preset")

        self.assertFalse(result["success"])
        self.assertIn("not found", result["message"])


class DemoPresetCommandTest(TestCase):
    """Tests for demo_presets management command."""

    def _call_command(self, *args, **kwargs):
        """Helper to call demo_presets command."""
        output = StringIO()
        error_output = StringIO()
        kwargs.setdefault("stdout", output)
        kwargs.setdefault("stderr", error_output)
        call_command("demo_presets", *args, **kwargs)
        return output.getvalue()

    def test_list_command_shows_available_presets(self):
        """Test that 'list' subcommand shows available presets."""
        output = self._call_command("list")

        self.assertIn("Available Demo Presets", output)
        self.assertIn("minimal_quickstart", output)
        self.assertIn("hpc_ai_platform", output)
        self.assertIn("government_cloud", output)
        self.assertIn("research_institution", output)

    def test_list_command_shows_preset_count(self):
        """Test that list command shows total preset count."""
        output = self._call_command("list")

        self.assertIn("preset(s) available", output)

    def test_info_command_shows_preset_details(self):
        """Test that 'info' subcommand shows preset details."""
        output = self._call_command("info", "minimal_quickstart")

        self.assertIn("minimal_quickstart", output)
        self.assertIn("Entity Counts", output)
        self.assertIn("Minimal Quickstart", output)

    def test_info_command_shows_scenarios(self):
        """Test that 'info' shows scenarios."""
        output = self._call_command("info", "minimal_quickstart")

        self.assertIn("Scenarios", output)

    def test_info_command_raises_error_for_invalid_preset(self):
        """Test that 'info' raises error for non-existent preset."""
        with self.assertRaises(CommandError) as context:
            self._call_command("info", "nonexistent_preset")

        self.assertIn("not found", str(context.exception))

    def test_help_is_shown_without_subcommand(self):
        """Test that help is shown when no subcommand is provided."""
        self._call_command()
        # The command should not raise an error, just show help
        # Output may be empty if help is sent to stderr in some Django versions


@ddt
class DemoPresetAPITest(test.APITestCase):
    """API tests for DemoPresetViewSet."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory(is_staff=False)
        self.base_url = "/api/marketplace-demo-presets/"
        self.list_url = f"{self.base_url}list/"

    def test_staff_can_list_presets(self):
        """Test that staff users can list presets."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.json(), list)
        self.assertGreaterEqual(len(response.json()), 4)

    def test_list_returns_expected_fields(self):
        """Test that list response contains expected fields."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        preset = response.json()[0]
        self.assertIn("name", preset)
        self.assertIn("title", preset)
        self.assertIn("description", preset)
        self.assertIn("version", preset)
        self.assertIn("entity_counts", preset)
        self.assertIn("scenarios", preset)

    def test_non_staff_cannot_list_presets(self):
        """Test that non-staff users cannot list presets."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_list_presets(self):
        """Test that anonymous users cannot list presets."""
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_can_retrieve_preset_details(self):
        """Test that staff can retrieve preset details."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(f"{self.base_url}info/minimal_quickstart/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], "minimal_quickstart")

    def test_retrieve_returns_expected_fields(self):
        """Test that retrieve response contains expected fields."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(f"{self.base_url}info/minimal_quickstart/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        data = response.json()
        self.assertEqual(data["name"], "minimal_quickstart")
        self.assertIn("Minimal Quickstart", data["title"])
        self.assertIsInstance(data["entity_counts"], dict)
        self.assertIsInstance(data["scenarios"], list)

    def test_non_staff_cannot_retrieve_preset(self):
        """Test that non-staff users cannot retrieve preset details."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(f"{self.base_url}info/minimal_quickstart/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_retrieve_returns_404_for_invalid_preset(self):
        """Test that retrieve returns 404 for non-existent preset."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(f"{self.base_url}info/nonexistent/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_non_staff_cannot_load_preset(self):
        """Test that non-staff users cannot load presets."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.post(
            f"{self.base_url}load/minimal_quickstart/",
            {"dry_run": True},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_load_preset(self):
        """Test that anonymous users cannot load presets."""
        response = self.client.post(
            f"{self.base_url}load/minimal_quickstart/",
            {"dry_run": True},
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_load_returns_404_for_invalid_preset(self):
        """Test that load returns 404 for non-existent preset."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.post(
            f"{self.base_url}load/nonexistent/",
            {"dry_run": True},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("hpc_ai_platform", "government_cloud", "research_institution")
    def test_all_presets_are_retrievable(self, preset_name):
        """Test that all presets can be retrieved."""
        self.client.force_authenticate(self.staff_user)
        response = self.client.get(f"{self.base_url}info/{preset_name}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["name"], preset_name)


@ddt
class DemoPresetLoadTest(test.APITestCase):
    """Integration tests for loading demo presets into the database."""

    @data(
        "minimal_quickstart",
        "government_cloud",
        "research_institution",
        "hpc_ai_platform",
        "call_management",
    )
    def test_preset_loads_successfully(self, preset_name):
        """Test that preset can be loaded into the database without errors."""
        result = DemoPresetManager.load_preset(
            preset_name,
            cleanup_first=True,
            dry_run=False,
            skip_users=False,
            skip_roles=False,
        )

        self.assertTrue(
            result["success"],
            f"Preset '{preset_name}' failed to load: {result['message']}\n"
            f"Output: {result.get('output', '')}",
        )
