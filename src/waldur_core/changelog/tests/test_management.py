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


@mock.patch(
    "waldur_core.changelog.management.commands.assemble_changelog.Command._find_project_root"
)
class AssembleChangelogPreviousReleaseTest(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.next_dir = Path(self.tmpdir) / "changelog" / "next"
        self.releases_dir = Path(self.tmpdir) / "changelog" / "releases"
        self.next_dir.mkdir(parents=True)

    def _fragment(self, title, commits=None):
        fragment = {
            "type": "fix",
            "category": "marketplace",
            "title": title,
            "description": "Test description",
            "scope": "core",
            "component": ["backend"],
            "impact": {"risk": "low"},
            "relevant_when": {"plugins": [], "feature_flags": [], "settings": []},
        }
        if commits is not None:
            fragment["commits"] = commits
        with open(self.next_dir / f"{title}.json", "w") as f:
            json.dump(fragment, f)

    def _previous(self, version, release_type, entries, base_stable="8.1.2"):
        path = Path(self.tmpdir) / f"{version}.json"
        with open(path, "w") as f:
            json.dump(
                {
                    "version": version,
                    "type": release_type,
                    "base_stable_version": base_stable,
                    "entries": entries,
                },
                f,
            )
        return str(path)

    def _assemble(self, version, release_type, **options):
        call_command(
            "assemble_changelog",
            release_version=version,
            date="2026-09-23",
            release_type=release_type,
            base_stable="8.1.2",
            **options,
        )
        with open(self.releases_dir / f"{version}.json") as f:
            return json.load(f)

    def test_without_previous_release_since_previous_equals_entries(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._fragment("a")
        release = self._assemble("8.1.3-rc.1", "rc", previous="8.1.2")
        self.assertEqual(release["since_previous"], release["entries"])
        self.assertEqual(release["previous_version"], "8.1.2")

    def test_rc_carries_previous_rc_entries(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._fragment("b")
        previous = self._previous(
            "8.1.3-rc.1", "rc", [{"id": "8.1.3-rc.1-1", "title": "a"}]
        )
        release = self._assemble(
            "8.1.3-rc.2", "rc", previous="8.1.3-rc.1", previous_release=previous
        )
        self.assertEqual(
            [e["id"] for e in release["entries"]], ["8.1.3-rc.1-1", "8.1.3-rc.2-1"]
        )
        self.assertEqual([e["id"] for e in release["since_previous"]], ["8.1.3-rc.2-1"])

    def test_first_rc_after_stable_does_not_carry(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._fragment("a")
        previous = self._previous("8.1.2", "stable", [{"id": "8.1.2-1"}], "8.1.1")
        release = self._assemble(
            "8.1.3-rc.1", "rc", previous="8.1.2", previous_release=previous
        )
        self.assertEqual([e["id"] for e in release["entries"]], ["8.1.3-rc.1-1"])

    def test_stable_since_previous_keeps_entries_with_unshipped_commits(
        self, mock_root
    ):
        mock_root.return_value = Path(self.tmpdir)
        self._fragment("a-shipped", commits=["abc1234"])
        self._fragment("b-new", commits=["def5678"])
        self._fragment("c-partly-new", commits=["abc1234", "0123456"])
        self._fragment("d-no-commits")
        previous = self._previous(
            "8.1.3-rc.2",
            "rc",
            # A longer abbreviation of the same commit still matches.
            [{"id": "8.1.3-rc.1-1", "commits": ["abc1234ef"]}],
        )
        release = self._assemble(
            "8.1.3", "stable", previous="8.1.3-rc.2", previous_release=previous
        )
        self.assertEqual(len(release["entries"]), 4)
        self.assertEqual(
            [e["title"] for e in release["since_previous"]],
            ["b-new", "c-partly-new", "d-no-commits"],
        )

    def test_previous_release_must_match_previous(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._fragment("a")
        previous = self._previous("8.1.3-rc.1", "rc", [])
        with self.assertRaisesRegex(CommandError, "--previous is 8.1.3-rc.2"):
            self._assemble(
                "8.1.3-rc.3", "rc", previous="8.1.3-rc.2", previous_release=previous
            )

    def test_missing_previous_release_file_fails(self, mock_root):
        mock_root.return_value = Path(self.tmpdir)
        self._fragment("a")
        with self.assertRaisesRegex(CommandError, "Cannot read --previous-release"):
            self._assemble(
                "8.1.3-rc.2", "rc", previous_release=f"{self.tmpdir}/missing.json"
            )
