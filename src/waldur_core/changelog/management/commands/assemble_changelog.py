import datetime
import json
import os
from pathlib import Path

import jsonschema
from django.core.management.base import BaseCommand, CommandError

from waldur_core.changelog.utils import parse_version

# Repo root: five levels up from this file (commands/ -> management/ -> changelog/
# -> waldur_core/ -> src/ -> repo root), where changelog/schema.json lives.
_SCHEMA_PATH = Path(__file__).resolve().parents[5] / "changelog" / "schema.json"


class Command(BaseCommand):
    help = "Assemble changelog fragments from changelog/next/ into a release file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--release-version",
            required=True,
            help="Release version (e.g., 8.0.8 or 8.0.8-rc.1)",
        )
        parser.add_argument(
            "--date",
            required=True,
            help="Release date in ISO 8601 format (e.g., 2026-04-15)",
        )
        parser.add_argument(
            "--release-type",
            default="stable",
            choices=["stable", "rc"],
            help="Release type (default: stable)",
        )
        parser.add_argument(
            "--base-stable",
            help="Previous stable version (for cumulative entries)",
        )
        parser.add_argument(
            "--previous",
            help="Immediately preceding version (for delta)",
        )
        parser.add_argument(
            "--stable-target",
            help="Target stable version (for RC releases)",
        )
        parser.add_argument(
            "--summary",
            default="",
            help="Release summary text",
        )
        parser.add_argument(
            "--no-clear",
            action="store_true",
            help="Do not clear changelog/next/ after assembly",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and print output without writing files",
        )

    def handle(self, *args, **options):
        version = options["release_version"]
        date = options["date"]
        release_type = options["release_type"]

        if parse_version(version) is None:
            raise CommandError(
                f"--release-version must be a valid version (e.g. 8.0.8 or "
                f"8.0.8-rc.1), got {version!r}"
            )
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            raise CommandError(
                f"--date must be a valid ISO 8601 date (e.g. 2026-04-15), got {date!r}"
            )

        # Find project root (where changelog/ directory lives)
        project_root = self._find_project_root()
        next_dir = project_root / "changelog" / "next"
        releases_dir = project_root / "changelog" / "releases"

        if not next_dir.exists():
            raise CommandError(f"Directory not found: {next_dir}")

        # Read fragments
        fragments = []
        for f in sorted(next_dir.glob("*.json")):
            try:
                with open(f) as fh:
                    fragment = json.load(fh)
                fragments.append((f.name, fragment))
            except json.JSONDecodeError as e:
                raise CommandError(f"Invalid JSON in {f.name}: {e}")

        if not fragments:
            raise CommandError(f"No changelog fragments found in {next_dir}")

        # Validate and assign IDs
        entry_schema = self._load_entry_schema()
        validator = jsonschema.Draft202012Validator(entry_schema)
        entries = []
        for i, (filename, fragment) in enumerate(fragments, 1):
            if not isinstance(fragment, dict):
                raise CommandError(
                    f"{filename}: expected a JSON object, got {type(fragment).__name__}"
                )
            fragment["id"] = f"{version}-{i}"
            self._validate_fragment(filename, fragment, validator)
            entries.append(fragment)

        # Build release file
        release = {
            "schema_version": "1.0.0",
            "version": version,
            "date": date,
            "type": release_type,
            "summary": options["summary"],
            "entries": entries,
        }

        if options["base_stable"]:
            release["base_stable_version"] = options["base_stable"]
        if options["previous"]:
            release["previous_version"] = options["previous"]
        if options["stable_target"]:
            release["stable_target"] = options["stable_target"]

        if options["dry_run"]:
            self.stdout.write(json.dumps(release, indent=2))
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nDry run: {len(entries)} entries validated successfully."
                )
            )
            return

        # Write release file
        releases_dir.mkdir(parents=True, exist_ok=True)
        output_path = releases_dir / f"{version}.json"
        with open(output_path, "w") as fh:
            json.dump(release, fh, indent=2)

        self.stdout.write(
            self.style.SUCCESS(f"Assembled {len(entries)} entries into {output_path}")
        )

        # Clear fragments
        if not options["no_clear"]:
            for f in next_dir.glob("*.json"):
                os.remove(f)
            self.stdout.write(self.style.SUCCESS(f"Cleared {next_dir}"))

    def _find_project_root(self):
        """Walk up from CWD to find the directory containing changelog/."""
        current = Path.cwd()
        for parent in [current, *current.parents]:
            if (parent / "changelog").is_dir() or (parent / "pyproject.toml").exists():
                return parent
        return current

    def _load_entry_schema(self):
        """Load the entry sub-schema from changelog/schema.json.

        Validating against the schema itself (instead of a hand-maintained copy
        of its rules) means the two can no longer drift apart.
        """
        with open(_SCHEMA_PATH) as fh:
            full_schema = json.load(fh)
        # $defs/entry uses relative $refs (e.g. "#/$defs/impact"), so carry the
        # full $defs along as this sub-schema's own root for them to resolve.
        return {**full_schema["$defs"]["entry"], "$defs": full_schema["$defs"]}

    def _validate_fragment(self, filename, fragment, validator):
        """Validate an assembled changelog entry against changelog/schema.json."""
        errors = sorted(validator.iter_errors(fragment), key=lambda e: e.message)

        # Not expressible in the schema: which fields are required depends on
        # "type", and JSON Schema has no first-class way to say that here.
        if fragment.get("type") == "security" and "security" not in fragment:
            errors.append(
                jsonschema.ValidationError(
                    "Security entries must include a 'security' object"
                )
            )

        if errors:
            formatted = [
                f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
                for e in errors
            ]
            raise CommandError(
                f"Validation errors in {filename}:\n"
                + "\n".join(f"  - {e}" for e in formatted)
            )
