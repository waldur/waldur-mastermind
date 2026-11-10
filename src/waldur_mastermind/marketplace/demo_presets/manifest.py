import contextlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from waldur_core.core.middleware import skip_side_effects
from waldur_core.core.models import User
from waldur_mastermind.invoices.models import Invoice
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.demo_presets import (
    order_history,
    proposal_history,
    settings_reset,
)
from waldur_mastermind.marketplace.demo_presets.credit_history import (
    generate_credit_history,
)
from waldur_mastermind.marketplace.demo_presets.gate import (
    overlay_name,
    staff_password,
)
from waldur_mastermind.marketplace.demo_presets.time_shift import (
    rebase_to_current_month,
)
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource

logger = logging.getLogger(__name__)

# Path to the default permissions.yaml file in the Docker container
# Falls back to the source location if not found
DEFAULT_PERMISSIONS_PATHS = [
    Path("/etc/waldur/permissions.yaml"),  # Docker/production
    Path(__file__).parent.parent.parent.parent.parent
    / "docker"
    / "rootfs"
    / "etc"
    / "waldur"
    / "permissions.yaml",  # Source tree
]


@dataclass
class PresetMetadata:
    """Metadata for a demo preset."""

    name: str
    title: str
    description: str
    version: str
    entity_counts: dict = field(default_factory=dict)
    scenarios: list = field(default_factory=list)


# A preset's name is its file's name: no separators, no leading dot.
PRESET_NAME = re.compile(r"^[\w-]+$")


class DemoPresetManager:
    """Manager for discovering and loading demo presets."""

    PRESETS_DIR = Path(__file__).parent / "presets"

    @classmethod
    def preset_dirs(cls) -> list[Path]:
        """Where presets are found, the first match winning: the deployment's
        own directories in the order given, then the built-in one."""
        extra = settings.WALDUR_CORE.get("DEMO_PRESETS_EXTRA_DIRS") or []
        return [Path(path) for path in extra] + [cls.PRESETS_DIR]

    @classmethod
    def _preset_files(cls) -> dict[str, Path]:
        """Every preset by name, the deployment's file replacing a built-in
        one of the same name."""
        found = {}
        for directory in cls.preset_dirs():
            if not directory.is_dir():
                logger.warning("Demo preset directory %s does not exist", directory)
                continue
            for file_path in sorted(directory.glob("*.json")):
                name = file_path.stem
                if not PRESET_NAME.match(name):
                    continue
                if name in found:
                    logger.info(
                        "Demo preset %s from %s replaces the one in %s",
                        name,
                        found[name].parent,
                        directory,
                    )
                    continue
                found[name] = file_path
        return found

    @classmethod
    def list_presets(cls) -> list[PresetMetadata]:
        """List all available presets with their metadata."""
        presets = []
        for preset_name in sorted(cls._preset_files()):
            metadata = cls.get_preset_info(preset_name)
            if metadata:
                presets.append(metadata)
        return presets

    @classmethod
    def load_preset_data(cls, name: str) -> dict | None:
        """The content of the preset ``name``, or None if there is no such
        preset or its file cannot be read as a JSON object."""
        file_path = cls.get_preset_path(name)
        if not file_path:
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            logger.warning(f"Failed to load preset {name}: {e}")
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def get_preset_info(cls, name: str) -> PresetMetadata | None:
        """Get metadata for a specific preset."""
        data = cls.load_preset_data(name)
        return None if data is None else cls.metadata_from(name, data)

    @classmethod
    def metadata_from(cls, name: str, data: dict) -> PresetMetadata:
        metadata = data.get("_metadata", {})
        return PresetMetadata(
            name=name,
            title=metadata.get("title", name.replace("_", " ").title()),
            description=metadata.get("description", ""),
            version=metadata.get("version", "1.0.0"),
            entity_counts=cls._count_entities(data),
            scenarios=metadata.get("scenarios", []),
        )

    @classmethod
    def _count_entities(cls, data: dict) -> dict:
        """Count entities in preset data."""
        counts = {}
        for key, value in data.items():
            if key.startswith("_"):  # Skip metadata fields
                continue
            if isinstance(value, list):
                counts[key] = len(value)
        return counts

    @classmethod
    def get_preset_path(cls, name: str) -> Path | None:
        """Get the file path for a preset."""
        if not name or not PRESET_NAME.match(name):
            return None
        for directory in cls.preset_dirs():
            file_path = directory / f"{name}.json"
            if file_path.is_file():
                return file_path
        return None

    @classmethod
    def get_preset_users(cls, name: str) -> list[dict]:
        """
        Get users with their passwords from a preset.

        Returns a list of dicts with username, password, and role info. Where
        the deployment sets the staff password, a staff user's password is
        left empty: the deployment's is never shown.
        """
        file_path = cls.get_preset_path(name)
        if not file_path:
            return []

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        hide_staff_passwords = bool(staff_password())
        users = data.get("users", [])
        return [
            {
                "username": user.get("username", ""),
                "password": ""
                if hide_staff_passwords and user.get("is_staff")
                else user.get("password", ""),
                "email": user.get("email", ""),
                "is_staff": user.get("is_staff", False),
                "is_support": user.get("is_support", False),
            }
            for user in users
            if user.get("username") and user.get("password")
        ]

    @classmethod
    def load_preset(
        cls,
        name: str,
        cleanup_first: bool = True,
        dry_run: bool = False,
        skip_users: bool = False,
        skip_roles: bool = False,
    ) -> dict:
        """
        Load a preset into the database.

        Args:
            name: Preset name (without .json extension)
            cleanup_first: Whether to run cleanup_structure before loading.
                A cleanup load also returns every setting and feature to its
                default, except the settings the deployment owns -- see
                demo_presets/settings_reset.py -- and then imports the
                deployment's overlay preset, if it sets one.
            dry_run: Preview changes without applying
            skip_users: Skip user import/cleanup
            skip_roles: Skip role import/cleanup

        Returns:
            dict with 'success', 'message', and 'output' keys
        """
        file_path = cls.get_preset_path(name)
        if not file_path:
            return {
                "success": False,
                "message": f"Preset '{name}' not found",
                "output": "",
            }

        overlay_path = None
        if cleanup_first and overlay_name() and overlay_name() != name:
            overlay_path = cls.get_preset_path(overlay_name())
            # Before anything is deleted: a load without the overlay would
            # leave the deployment without what it needs.
            if not overlay_path:
                return {
                    "success": False,
                    "message": f"Overlay preset '{overlay_name()}' not found",
                    "output": "",
                }
            if cls.load_preset_data(overlay_name()) is None:
                return {
                    "success": False,
                    "message": f"Overlay preset '{overlay_name()}' cannot be read",
                    "output": "",
                }

        output = StringIO()

        try:
            # Nothing in a load may notify anyone, publish to site agents or
            # queue billing: the generators below save orders, usages and
            # invoices through the ORM, so their signal handlers would.
            with skip_side_effects():
                cls._load(
                    file_path,
                    output,
                    cleanup_first,
                    dry_run,
                    skip_users,
                    skip_roles,
                    overlay_path=overlay_path,
                )

            # Get users with passwords for the response
            users = cls.get_preset_users(name)

            # Print user credentials in output
            if users:
                output.write(
                    "\n============================================================\n"
                )
                output.write("Demo User Credentials\n")
                output.write(
                    "============================================================\n"
                )
                for user in users:
                    role = ""
                    if user.get("is_staff"):
                        role = " [staff]"
                    elif user.get("is_support"):
                        role = " [support]"
                    password = user["password"] or "(the deployment's staff password)"
                    output.write(f"  {user['username']}: {password}{role}\n")
                output.write(
                    "============================================================\n"
                )

            # import_structure reports per-entity failures as warnings and still
            # exits 0, so a preset can lose whole collections while the load looks
            # clean. Surface that here rather than letting it pass as success.
            error_count = cls._count_import_errors(output.getvalue())
            if error_count:
                return {
                    "success": False,
                    "message": (
                        f"Preset '{name}' loaded with {error_count} failed "
                        f"entit{'y' if error_count == 1 else 'ies'} — see output"
                    ),
                    "output": output.getvalue(),
                    "users": users,
                }

            return {
                "success": True,
                "message": f"Preset '{name}' loaded successfully"
                + (" (dry run)" if dry_run else ""),
                "output": output.getvalue(),
                "users": users,
            }
        except Exception as e:
            logger.exception(f"Failed to load preset {name}")
            return {
                "success": False,
                "message": str(e),
                "output": output.getvalue(),
                "users": [],
            }

    @classmethod
    def _load(
        cls,
        file_path,
        output,
        cleanup_first,
        dry_run,
        skip_users,
        skip_roles,
        overlay_path=None,
    ):
        if cleanup_first:
            # One transaction: the cleanup's TRUNCATE CASCADE empties the
            # settings table, so the deployment's settings must be back
            # before anything commits; and a strict failure rolls back every
            # table already emptied rather than leave a half-cleaned database.
            with transaction.atomic():
                owned = [] if dry_run else settings_reset.snapshot()
                call_command(
                    "cleanup_structure",
                    skip_users=skip_users,
                    skip_roles=skip_roles,
                    fast=True,  # Use fast raw SQL DELETE for speed
                    skip_rabbitmq_messages=True,
                    strict=True,
                    dry_run=dry_run,
                    stdout=output,
                )
                if not dry_run:
                    settings_reset.reset(owned)
                    output.write(
                        "Settings and features reset to their defaults; kept "
                        f"{len(owned)} the deployment owns.\n"
                    )

        # Ensure system roles exist before importing user_roles
        # This is normally done by initdb in production
        if not dry_run and not skip_roles:
            cls._ensure_system_roles_exist(output)

        cls._import(file_path, output, dry_run, skip_users, skip_roles)
        # The overlay goes in before the generators, which work on the whole
        # database: run after them, it would bill everything a second time.
        # Imported last, its settings and features win over the preset's.
        if overlay_path:
            output.write(f"\nImporting the deployment's overlay {overlay_path.stem}\n")
            cls._import(overlay_path, output, dry_run, skip_users, skip_roles)

        if not dry_run and not skip_users:
            cls._set_staff_password(output, file_path, overlay_path)

        # Generate invoices for imported resources (if not dry run)
        if not dry_run:
            # Before billing: a resource is billed from the orders that
            # made it, as in production.
            order_history.generate_order_history(output)
            proposal_history.generate_proposal_history(output)
            cls._generate_billing_for_resources(output)
            cls._generate_credit_history(output, file_path)
            settings_reset.clear_configuration_cache()

    @classmethod
    def _import(cls, file_path, output, dry_run, skip_users, skip_roles):
        # With its billing history moved onto the current month -- see
        # demo_presets/time_shift.py.
        with cls._rebased_preset_file(file_path) as import_path:
            call_command(
                "import_structure",
                input=str(import_path),
                skip_users=skip_users,
                skip_roles=skip_roles,
                skip_rabbitmq_messages=True,
                protect_owned_settings=True,
                dry_run=dry_run,
                stdout=output,
            )

    @staticmethod
    def _set_staff_password(output, *file_paths):
        """Give the loaded presets' staff users the deployment's password, so
        the one the preset publishes does not open a staff account."""
        password = staff_password()
        if not password:
            return
        usernames = set()
        for file_path in file_paths:
            if not file_path:
                continue
            data = json.loads(file_path.read_text())
            usernames.update(
                user["username"]
                for user in data.get("users", [])
                if user.get("is_staff") and user.get("username")
            )
        users = User.objects.filter(username__in=usernames, is_staff=True)
        for user in users:
            user.set_password(password)
            user.save(update_fields=["password"])
        output.write(
            f"Gave {len(users)} staff user(s) the deployment's staff password.\n"
        )

    @staticmethod
    @contextlib.contextmanager
    def _rebased_preset_file(file_path: Path):
        """Yield a preset file whose billing history ends this month.

        The committed file is never rewritten: a preset that needs no shift
        is imported as it stands, and one that does is written to a
        temporary copy for the duration of the import.
        """
        data = json.loads(file_path.read_text())
        rebased = rebase_to_current_month(data)
        if rebased is data:
            yield file_path
            return

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(rebased, handle)
            temporary = Path(handle.name)
        try:
            yield temporary
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _count_import_errors(output: str) -> int:
        """Count per-entity import failures reported by import_structure.

        The command writes one "Failed to import <thing> <id>: <error>" line per
        failed row and then exits 0, so the count has to be recovered from the
        captured output.
        """
        return sum(
            1
            for line in output.splitlines()
            if line.lstrip().startswith("Failed to import ")
        )

    @classmethod
    def _generate_billing_for_resources(cls, output: StringIO):
        """
        Generate billing (invoices, invoice items) for all active resources.

        This method triggers the billing system for each resource that is in
        the OK state, creating the appropriate invoices and invoice items
        based on the resource's plan components.

        Creating a customer's invoice for the month bills every active resource
        of that customer. Once one resource has created it, the customer's
        other resources are already on it, and registering them again would
        bill them twice. Resources are registered one by one only on an
        invoice the preset ships.
        """
        output.write("\n============================================================\n")
        output.write("Generating Billing Data\n")
        output.write("============================================================\n")

        resources = Resource.objects.filter(state=ResourceStates.OK).select_related(
            "project"
        )
        invoice_count = 0
        today = timezone.now()
        invoiced_customers = set()

        for resource in resources:
            customer_id = resource.project.customer_id
            try:
                if customer_id not in invoiced_customers:
                    invoice_exists = Invoice.objects.filter(
                        customer_id=customer_id, year=today.year, month=today.month
                    ).exists()
                    MarketplaceBillingService.handle_resource_creation(resource)
                    if not invoice_exists:
                        invoiced_customers.add(customer_id)
                invoice_count += 1
                output.write(f"Generated billing for: {resource.name}\n")
            except Exception as e:
                logger.warning(f"Failed to generate billing for {resource.name}: {e}")
                output.write(
                    f"Warning: Failed to generate billing for {resource.name}: {e}\n"
                )

        output.write(f"\nGenerated billing for {invoice_count} resources\n")

    @classmethod
    def _generate_credit_history(cls, output: StringIO, file_path: Path = None):
        """
        Generate historical credit consumption for credited customers.

        No-op for presets without credits. Past months are billed and then run
        through the real compensation flow, so compensations, minimal-consumption
        draws and credit balances match what production would produce.

        A preset may shape individual projects through `_metadata.credit_history`;
        see generate_credit_history.
        """
        patterns = {}
        if file_path:
            try:
                with open(file_path) as preset_file:
                    metadata = json.load(preset_file).get("_metadata") or {}
                patterns = metadata.get("credit_history") or {}
            except (OSError, ValueError) as e:
                logger.warning(f"Failed to read credit history patterns: {e}")

        try:
            processed = generate_credit_history(stdout=output, patterns=patterns)
        except Exception as e:
            logger.warning(f"Failed to generate credit history: {e}")
            output.write(f"Warning: Failed to generate credit history: {e}\n")
            return

        if not processed:
            return

        output.write("\n============================================================\n")
        output.write("Generated Credit History\n")
        output.write("============================================================\n")
        output.write(f"Compensated {processed} customer-months\n")

    @classmethod
    def _get_permissions_file(cls) -> Path | None:
        """Find the permissions.yaml file."""
        for path in DEFAULT_PERMISSIONS_PATHS:
            if path.exists():
                return path
        return None

    @classmethod
    def _ensure_system_roles_exist(cls, output: StringIO):
        """
        Ensure required system roles exist in the database.

        Uses the import_roles command with permissions.yaml to create system
        roles (CUSTOMER.OWNER, PROJECT.MANAGER, etc.) with their full
        permission sets. This mirrors how roles are created in production
        by the initdb script.
        """
        output.write("\n============================================================\n")
        output.write("Importing System Roles\n")
        output.write("============================================================\n")

        permissions_file = cls._get_permissions_file()
        if not permissions_file:
            output.write("Warning: permissions.yaml not found, skipping role import\n")
            logger.warning("permissions.yaml not found in any of the default paths")
            return

        try:
            output.write(f"Using permissions file: {permissions_file}\n")
            call_command("import_roles", str(permissions_file), stdout=output)
            output.write("\nSystem roles imported successfully\n")
        except Exception as e:
            logger.warning(f"Failed to import roles: {e}")
            output.write(f"Warning: Failed to import roles: {e}\n")

    @classmethod
    def export_to_preset(
        cls,
        name: str,
        title: str = "",
        description: str = "",
        output_path: str | None = None,
    ) -> dict:
        """
        Export current database state as a preset.

        Args:
            name: Name for the new preset
            title: Human-readable title
            description: Description of the preset
            output_path: Optional custom output path

        Returns:
            dict with 'success', 'message', and 'path' keys
        """
        if output_path:
            file_path = Path(output_path)
        else:
            file_path = cls.PRESETS_DIR / f"{name}.json"

        output = StringIO()

        try:
            call_command(
                "export_structure",
                output=str(file_path),
                stdout=output,
            )

            # Add metadata to the exported file
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)

            data["_metadata"] = {
                "title": title or name.replace("_", " ").title(),
                "description": description or f"Exported preset: {name}",
                "version": "1.0.0",
                "scenarios": [],
            }

            # Rewrite with metadata at the top
            ordered_data = {"_metadata": data.pop("_metadata")}
            ordered_data.update(data)

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(ordered_data, f, indent=2, ensure_ascii=False)

            return {
                "success": True,
                "message": f"Exported to '{file_path}'",
                "path": str(file_path),
                "output": output.getvalue(),
            }
        except Exception as e:
            logger.exception(f"Failed to export preset {name}")
            return {
                "success": False,
                "message": str(e),
                "output": output.getvalue(),
            }
