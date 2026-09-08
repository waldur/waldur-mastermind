import collections
import datetime
import decimal
import json
import pathlib
import uuid
from io import StringIO
from unittest import mock

from ddt import data, ddt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.demo_presets.manifest import (
    DemoPresetManager,
    PresetMetadata,
)
from waldur_mastermind.marketplace.demo_presets.time_shift import (
    rebase_to_current_month,
)


def _billing_preset(*, opt_in=True):
    """One invoice month so the shift's effect on each field is visible."""
    preset = {
        "_metadata": {"title": "probe"},
        "invoices": [
            {
                "uuid": "11" * 16,
                "customer_uuid": "a3" + "0" * 30,
                "year": 2025,
                "month": 12,
                "created": "2025-12-01",
                "invoice_date": "2025-12-15",
            }
        ],
        "invoice_items": [
            {
                "uuid": "22" * 16,
                "invoice_uuid": "11" * 16,
                "start": "2025-12-01T00:00:00",
                "end": "2025-12-31T23:59:59",
            }
        ],
    }
    if opt_in:
        preset["_metadata"]["rebase_billing_history"] = True
    return preset


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

    def test_every_preset_uuid_is_parseable(self):
        """No preset may carry a uuid that UUIDField cannot parse.

        core.fields.UUIDField coerces an unparseable value to None instead of
        raising, so a malformed uuid in a preset is stored as NULL and only
        surfaces as a not-null violation on insert -- an error that names
        neither the value nor the row. hpc_ai_platform shipped 1039 of them
        (34-character component usage ids, and order ids beginning "o3"),
        which silently dropped every order and every usage row it defined.
        """
        offenders = []
        for metadata in DemoPresetManager.list_presets():
            path = DemoPresetManager.get_preset_path(metadata.name)
            payload = json.loads(pathlib.Path(path).read_text())

            def visit(node, trail):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key == "uuid" and isinstance(value, str) and value:
                            try:
                                uuid.UUID(value.replace("-", ""))
                            except ValueError:
                                offenders.append(
                                    f"{metadata.name}: {trail}.{key} = {value!r}"
                                )
                        else:
                            visit(value, f"{trail}.{key}")
                elif isinstance(node, list):
                    for index, item in enumerate(node):
                        visit(item, f"{trail}[{index}]")

            visit(payload, "$")

        self.assertEqual(offenders, [], "\n".join(offenders[:20]))

    def test_hpc_ai_platform_usage_rows_reference_real_component_usages(self):
        """component_user_usages must point at component_usages that exist.

        The uuid repair renumbered both collections; this pins the references
        so a future renumbering cannot orphan them.
        """
        path = DemoPresetManager.get_preset_path("hpc_ai_platform")
        payload = json.loads(pathlib.Path(path).read_text())

        usage_uuids = {row["uuid"] for row in payload["component_usages"]}
        referenced = {
            row["component_usage_uuid"] for row in payload["component_user_usages"]
        }

        self.assertTrue(usage_uuids)
        self.assertTrue(referenced)
        self.assertEqual(referenced - usage_uuids, set())

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


class PresetMonthRebaseTest(TestCase):
    """A preset's billing history must land on the month it is loaded in.

    The generator anchors months on ``date.today()`` and writes them
    absolutely, so a committed preset ages: scenarios asking about "last
    month" reach a month it holds no data for, and the assistant is scored
    against an empty answer it gave correctly.
    """

    def setUp(self):
        path = DemoPresetManager.get_preset_path("credit_realistic")
        self.preset = json.loads(path.read_text())

    @staticmethod
    def _months(data):
        return sorted({(i["year"], i["month"]) for i in data["invoices"]})

    def test_newest_month_follows_the_load_date(self):
        for today in (datetime.date(2026, 9, 8), datetime.date(2027, 2, 1)):
            with self.subTest(today=today):
                shifted = rebase_to_current_month(self.preset, today=today)
                self.assertEqual(self._months(shifted)[-1], (today.year, today.month))

    def test_relative_spacing_is_preserved(self):
        before = self._months(self.preset)
        shifted = self._months(
            rebase_to_current_month(self.preset, today=datetime.date(2027, 2, 1))
        )
        self.assertEqual(len(shifted), len(before))
        gaps = {
            (b[0] - a[0]) * 12 + (b[1] - a[1])
            for a, b in zip(shifted, shifted[1:], strict=False)
        }
        self.assertEqual(gaps, {1})

    def test_a_month_end_clamps_to_the_shorter_month(self):
        # Items end on the last instant of their month; December's 31st has
        # to become February's 28th, not spill into March.
        shifted = rebase_to_current_month(
            _billing_preset(), today=datetime.date(2026, 2, 1)
        )
        self.assertEqual(shifted["invoice_items"][0]["end"], "2026-02-28T23:59:59")

    def test_the_terminated_resource_is_billed_in_every_month(self):
        # support_compensations/terminated_billed_visibility asks whether a
        # terminated resource was billed last month; it can only answer if
        # the history reaches that far.
        shifted = rebase_to_current_month(self.preset, today=datetime.date(2026, 9, 8))
        term = next(r for r in shifted["resources"] if "Sirius VM 1" in r["name"])
        invoices = {i["uuid"]: (i["year"], i["month"]) for i in shifted["invoices"]}
        billed = {
            invoices[i["invoice_uuid"]]
            for i in shifted["invoice_items"]
            if i.get("resource_uuid") == term["uuid"]
        }
        self.assertEqual(billed, set(self._months(shifted)))

    def test_credit_expiry_is_left_alone(self):
        # end_date is a future expiry, not history; shifting it would move
        # an expiry that has nothing to do with the billing window.
        shifted = rebase_to_current_month(self.preset, today=datetime.date(2027, 2, 1))
        self.assertEqual(
            [c["end_date"] for c in shifted["customer_credits"]],
            [c["end_date"] for c in self.preset["customer_credits"]],
        )

    def test_the_source_preset_is_not_mutated(self):
        before = self._months(self.preset)
        rebase_to_current_month(self.preset, today=datetime.date(2027, 2, 1))
        self.assertEqual(self._months(self.preset), before)

    def test_load_feeds_import_structure_a_rebased_file(self):
        # The rebase is worthless if the loader still hands the committed
        # file straight to import_structure.
        imported = {}

        def record(command, **kwargs):
            if command == "import_structure":
                imported["months"] = self._months(
                    json.loads(pathlib.Path(kwargs["input"]).read_text())
                )

        with mock.patch(
            "waldur_mastermind.marketplace.demo_presets.manifest.call_command",
            side_effect=record,
        ):
            DemoPresetManager.load_preset("credit_realistic", dry_run=True)

        today = timezone.localdate()
        self.assertEqual(imported["months"][-1], (today.year, today.month))

    def test_a_preset_that_does_not_opt_in_is_left_alone(self):
        # Four other presets carry invoices; the loader is shared, so a
        # shift they never asked for moved their history and left their
        # invoice_date, ledger and policy timestamps behind.
        preset = _billing_preset(opt_in=False)
        shifted = rebase_to_current_month(preset, today=datetime.date(2027, 2, 1))
        self.assertIs(shifted, preset)

    def test_invoice_date_moves_with_the_invoice(self):
        # invoice_date is imported and drives due_date; left behind, every
        # shifted invoice read as months overdue.
        shifted = rebase_to_current_month(
            _billing_preset(), today=datetime.date(2026, 2, 1)
        )
        self.assertEqual(shifted["invoices"][0]["invoice_date"], "2026-02-15")

    def test_load_leaves_a_non_opted_in_preset_as_committed(self):
        imported = {}

        def record(command, **kwargs):
            if command == "import_structure":
                imported["months"] = self._months(
                    json.loads(pathlib.Path(kwargs["input"]).read_text())
                )

        committed = self._months(
            json.loads(DemoPresetManager.get_preset_path("hpc_ai_platform").read_text())
        )
        with mock.patch(
            "waldur_mastermind.marketplace.demo_presets.manifest.call_command",
            side_effect=record,
        ):
            DemoPresetManager.load_preset("hpc_ai_platform", dry_run=True)

        self.assertEqual(imported["months"], committed)

    def test_today_defaults_to_the_django_clock(self):
        # credit_history, billing._register and build_context all read
        # Django's clock; date.today() is the machine's, which can be a
        # month ahead of it for three hours around midnight on the 1st.
        with mock.patch(
            "waldur_mastermind.marketplace.demo_presets.time_shift.timezone"
        ) as tz:
            tz.localdate.return_value = datetime.date(2027, 2, 1)
            shifted = rebase_to_current_month(self.preset)
        self.assertEqual(self._months(shifted)[-1], (2027, 2))
