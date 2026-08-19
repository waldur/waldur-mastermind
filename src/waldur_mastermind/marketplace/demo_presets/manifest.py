import json
import logging
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from django.core.management import call_command

from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.demo_presets.credit_history import (
    generate_credit_history,
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


class DemoPresetManager:
    """Manager for discovering and loading demo presets."""

    PRESETS_DIR = Path(__file__).parent / "presets"

    @classmethod
    def list_presets(cls) -> list[PresetMetadata]:
        """List all available presets with their metadata."""
        presets = []
        for file_path in sorted(cls.PRESETS_DIR.glob("*.json")):
            preset_name = file_path.stem
            metadata = cls.get_preset_info(preset_name)
            if metadata:
                presets.append(metadata)
        return presets

    @classmethod
    def get_preset_info(cls, name: str) -> PresetMetadata | None:
        """Get metadata for a specific preset."""
        file_path = cls.PRESETS_DIR / f"{name}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load preset {name}: {e}")
            return None

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
        file_path = cls.PRESETS_DIR / f"{name}.json"
        return file_path if file_path.exists() else None

    @classmethod
    def get_preset_users(cls, name: str) -> list[dict]:
        """
        Get users with their passwords from a preset.

        Returns a list of dicts with username, password, and role info.
        """
        file_path = cls.get_preset_path(name)
        if not file_path:
            return []

        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

        users = data.get("users", [])
        return [
            {
                "username": user.get("username", ""),
                "password": user.get("password", ""),
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
            cleanup_first: Whether to run cleanup_structure before loading
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

        output = StringIO()

        try:
            # Optionally cleanup existing data
            if cleanup_first:
                cleanup_args = {
                    "skip_users": skip_users,
                    "skip_roles": skip_roles,
                    "fast": True,  # Use fast raw SQL DELETE for speed
                    "stdout": output,
                }
                if dry_run:
                    cleanup_args["dry_run"] = True

                call_command("cleanup_structure", **cleanup_args)

            # Ensure system roles exist before importing user_roles
            # This is normally done by initdb in production
            if not dry_run and not skip_roles:
                cls._ensure_system_roles_exist(output)

            # Import the preset data
            import_args = {
                "input": str(file_path),
                "skip_users": skip_users,
                "skip_roles": skip_roles,
                "skip_rabbitmq_messages": True,
                "stdout": output,
            }
            if dry_run:
                import_args["dry_run"] = True

            call_command("import_structure", **import_args)

            # Generate invoices for imported resources (if not dry run)
            if not dry_run:
                cls._generate_billing_for_resources(output)
                cls._generate_credit_history(output, file_path)

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
                    output.write(f"  {user['username']}: {user['password']}{role}\n")
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
        """
        output.write("\n============================================================\n")
        output.write("Generating Billing Data\n")
        output.write("============================================================\n")

        resources = Resource.objects.filter(state=ResourceStates.OK)
        invoice_count = 0

        for resource in resources:
            try:
                MarketplaceBillingService.handle_resource_creation(resource)
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
