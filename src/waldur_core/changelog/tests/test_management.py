import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


class AssembleChangelogTest(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.next_dir = Path(self.tmpdir) / "changelog" / "next"
        self.releases_dir = Path(self.tmpdir) / "changelog" / "releases"
        self.next_dir.mkdir(parents=True)
        self.releases_dir.mkdir(parents=True)

    def _write_fragment(self, filename, data):
        with open(self.next_dir / filename, "w") as f:
            json.dump(data, f)

    def _valid_fragment(self, **overrides):
        fragment = {
            "type": "feature",
            "category": "marketplace",
            "title": "Test feature",
            "description": "Test description",
            "scope": "core",
            "component": ["backend"],
            "impact": {"risk": "low"},
            "relevant_when": {"plugins": [], "feature_flags": [], "settings": []},
        }
        fragment.update(overrides)
        return fragment

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_assembles_fragments(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("feature-a.json", self._valid_fragment(title="Feature A"))
        self._write_fragment(
            "fix-b.json", self._valid_fragment(type="fix", title="Fix B")
        )

        call_command(
            "assemble_changelog",
            release_version="8.0.8",
            date="2026-04-15",
        )

        output_path = self.releases_dir / "8.0.8.json"
        self.assertTrue(output_path.exists())

        with open(output_path) as f:
            release = json.load(f)
        self.assertEqual(release["version"], "8.0.8")
        self.assertEqual(len(release["entries"]), 2)
        self.assertEqual(release["entries"][0]["id"], "8.0.8-1")
        self.assertEqual(release["entries"][1]["id"], "8.0.8-2")

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_validates_invalid_type(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("bad.json", self._valid_fragment(type="invalid"))

        with self.assertRaises(CommandError):
            call_command(
                "assemble_changelog", release_version="8.0.8", date="2026-04-15"
            )

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_security_requires_security_object(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("sec.json", self._valid_fragment(type="security"))

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "assemble_changelog", release_version="8.0.8", date="2026-04-15"
            )
        self.assertIn("security", str(ctx.exception).lower())

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_security_with_valid_security_object(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        fragment = self._valid_fragment(
            type="security",
            security={
                "urgency": "high",
                "affected_versions": "< 8.0.8",
                "exploitability": "Requires auth",
                "mitigation": "Upgrade to 8.0.8",
            },
        )
        self._write_fragment("sec.json", fragment)

        call_command("assemble_changelog", release_version="8.0.8", date="2026-04-15")

        output_path = self.releases_dir / "8.0.8.json"
        self.assertTrue(output_path.exists())

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_dry_run_does_not_write(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("feature.json", self._valid_fragment())

        call_command(
            "assemble_changelog",
            release_version="8.0.8",
            date="2026-04-15",
            dry_run=True,
        )

        output_path = self.releases_dir / "8.0.8.json"
        self.assertFalse(output_path.exists())

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_clears_next_after_assembly(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("feature.json", self._valid_fragment())

        call_command("assemble_changelog", release_version="8.0.8", date="2026-04-15")

        remaining = list(self.next_dir.glob("*.json"))
        self.assertEqual(len(remaining), 0)

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_no_clear_preserves_fragments(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("feature.json", self._valid_fragment())

        call_command(
            "assemble_changelog",
            release_version="8.0.8",
            date="2026-04-15",
            no_clear=True,
        )

        remaining = list(self.next_dir.glob("*.json"))
        self.assertEqual(len(remaining), 1)

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_rejects_invalid_version(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("feature.json", self._valid_fragment())

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "assemble_changelog", release_version="not-a-version", date="2026-04-15"
            )
        self.assertIn("--release-version", str(ctx.exception))

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_rejects_invalid_date(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("feature.json", self._valid_fragment())

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "assemble_changelog", release_version="8.0.8", date="15-04-2026"
            )
        self.assertIn("--date", str(ctx.exception))

    @mock.patch(
        "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
    )
    def test_rejects_non_dict_fragment(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._write_fragment("list.json", ["not", "an", "object"])

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "assemble_changelog", release_version="8.0.8", date="2026-04-15"
            )
        self.assertIn("expected a JSON object", str(ctx.exception))
