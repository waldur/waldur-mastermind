import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace.models import (
    SoftwareCatalog,
    SoftwarePackage,
    SoftwareTarget,
    SoftwareVersion,
)


class Command(BaseCommand):
    help = "Load EESSI software catalog data into marketplace software catalog models"

    def add_arguments(self, parser):
        parser.add_argument(
            "--json-file",
            type=str,
            default="eessi.model.json",
            help="Path to EESSI JSON file (default: eessi.model.json)",
        )
        parser.add_argument(
            "--catalog-name",
            type=str,
            default="EESSI",
            help="Name of the software catalog (default: EESSI)",
        )
        parser.add_argument(
            "--catalog-version",
            type=str,
            help="EESSI catalog version (e.g., 2023.06). If not provided, will try to extract from JSON",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            help="Update existing catalog data if it exists",
        )
        parser.add_argument(
            "--no-sync",
            action="store_true",
            help="Do not remove records missing from JSON file (default: sync enabled)",
        )

    def handle(self, *args, **options):
        json_file = options["json_file"]
        catalog_name = options["catalog_name"]
        catalog_version = options["catalog_version"]
        dry_run = options["dry_run"]
        update_existing = options["update_existing"]
        sync_enabled = not options["no_sync"]

        # Check if file exists
        if not os.path.isfile(json_file):
            # Try relative to project root
            project_root = Path(__file__).parent.parent.parent.parent.parent.parent
            json_file = project_root / json_file

        if not os.path.isfile(json_file):
            raise CommandError(f"JSON file not found: {json_file}")

        self.stdout.write(f"Loading EESSI data from: {json_file}")

        # Load and parse JSON
        try:
            with open(json_file) as f:
                eessi_data = json.load(f)
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON file: {e}")

        # Extract version from data if not provided
        if not catalog_version:
            catalog_version = self._extract_version_from_data(eessi_data)
            if not catalog_version:
                catalog_version = "unknown"
                self.stdout.write(
                    self.style.WARNING(
                        "Could not determine version from data, using 'unknown'"
                    )
                )

        # Extract supported architectures from targets
        supported_architectures = self._extract_architectures(
            eessi_data.get("targets", [])
        )

        if dry_run:
            self._show_dry_run_info(
                catalog_name,
                catalog_version,
                supported_architectures,
                eessi_data,
                sync_enabled,
            )
            return

        # Create or get software catalog
        catalog, created = SoftwareCatalog.objects.get_or_create(
            name=catalog_name,
            version=catalog_version,
            defaults={
                "source_url": "https://software.eessi.io/",
                "description": f"European Environment for Scientific Software Installations {catalog_version}",
            },
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created new software catalog: {catalog_name} {catalog_version}"
                )
            )
        elif update_existing:
            catalog.source_url = "https://software.eessi.io/"
            catalog.description = f"European Environment for Scientific Software Installations {catalog_version}"
            catalog.save()
            self.stdout.write("Updated existing software catalog")
        else:
            self.stdout.write(
                self.style.WARNING(
                    "Software catalog already exists. Use --update-existing to update it."
                )
            )
            return

        # Load software packages with synchronization
        software_data = eessi_data.get("software", {})
        packages_created = 0
        versions_created = 0
        targets_created = 0
        packages_updated = 0
        packages_deleted = 0
        versions_deleted = 0
        targets_deleted = 0

        # Track which packages, versions, and targets should exist
        json_package_names = set(software_data.keys())
        json_versions_by_package = {}
        json_targets_by_version = {}

        # Process all packages from JSON
        for package_name, package_info in software_data.items():
            # Create or update software package
            package, pkg_created = SoftwarePackage.objects.get_or_create(
                catalog=catalog,
                name=package_name,
                defaults={
                    "description": package_info.get("description", ""),
                    "homepage": package_info.get("homepage", ""),
                },
            )
            if pkg_created:
                packages_created += 1
            elif update_existing:
                # Update package info
                package.description = package_info.get("description", "")
                package.homepage = package_info.get("homepage", "")
                package.save()
                packages_updated += 1

            # Track versions for this package
            versions_data = package_info.get("versions", {})
            json_version_names = set(versions_data.keys())
            json_versions_by_package[package.id] = json_version_names

            # Process versions
            for version_name, version_info in versions_data.items():
                version, ver_created = SoftwareVersion.objects.get_or_create(
                    package=package,
                    version=version_name,
                    defaults={
                        "release_date": None,  # EESSI data doesn't include release dates
                    },
                )
                if ver_created:
                    versions_created += 1

                # Track targets for this version
                json_targets_by_version[version.id] = set(supported_architectures)

                # Create software targets for each supported architecture
                for arch in supported_architectures:
                    # For EESSI, most targets use 'generic' CPU microarchitecture
                    cpu_microarchitecture = "generic"
                    path = f"/cvmfs/software.eessi.io/versions/{catalog_version}/software/linux/{arch}/{cpu_microarchitecture}"
                    target, tgt_created = SoftwareTarget.objects.get_or_create(
                        version=version,
                        cpu_family=arch,
                        cpu_microarchitecture=cpu_microarchitecture,
                        defaults={
                            "path": path,
                        },
                    )
                    if tgt_created:
                        targets_created += 1

        # Synchronization: Remove packages/versions/targets not in JSON
        if sync_enabled:
            # Remove packages not in JSON
            packages_to_delete = catalog.packages.exclude(name__in=json_package_names)
            packages_deleted = packages_to_delete.count()
            packages_to_delete.delete()

            # Remove versions not in JSON (for remaining packages)
            for package_id, json_version_names in json_versions_by_package.items():
                versions_to_delete = SoftwareVersion.objects.filter(
                    package_id=package_id
                ).exclude(version__in=json_version_names)
                versions_deleted += versions_to_delete.count()
                versions_to_delete.delete()

            # Remove targets not in JSON (for remaining versions)
            for version_id, json_target_archs in json_targets_by_version.items():
                targets_to_delete = SoftwareTarget.objects.filter(
                    version_id=version_id
                ).exclude(cpu_family__in=json_target_archs)
                targets_deleted += targets_to_delete.count()
                targets_to_delete.delete()

        # Summary
        summary_lines = [
            "\nEESSI catalog loaded successfully:",
            f"  Catalog: {catalog_name} {catalog_version} ({catalog.uuid})",
            f"  Packages created: {packages_created}",
            f"  Versions created: {versions_created}",
            f"  Targets created: {targets_created}",
        ]

        if update_existing and packages_updated > 0:
            summary_lines.append(f"  Packages updated: {packages_updated}")

        if sync_enabled:
            summary_lines.extend(
                [
                    f"  Packages deleted: {packages_deleted}",
                    f"  Versions deleted: {versions_deleted}",
                    f"  Targets deleted: {targets_deleted}",
                ]
            )

        summary_lines.append(f"  Architectures: {', '.join(supported_architectures)}")

        self.stdout.write(self.style.SUCCESS("\n".join(summary_lines)))

        self.stdout.write(
            self.style.SUCCESS(
                f"\nTo associate this catalog with an offering:\n"
                f"  Use the API endpoint: /api/marketplace-offering-software-catalogs/\n"
                f"  Catalog UUID: {catalog.uuid}\n"
                f"  Available architectures: {', '.join(supported_architectures)}"
            )
        )

    def _extract_version_from_data(self, eessi_data):
        """Extract version from EESSI data paths."""
        targets = eessi_data.get("targets", [])
        if targets:
            # Extract version from path like "/cvmfs/software.eessi.io/versions/2023.06/..."
            for target in targets:
                if "/versions/" in target:
                    parts = target.split("/versions/")
                    if len(parts) > 1:
                        version_part = parts[1].split("/")[0]
                        return version_part
        return None

    def _extract_architectures(self, targets):
        """Extract unique architectures from target paths."""
        architectures = set()
        for target in targets:
            if "/linux/" in target:
                # Extract architecture from path like "/.../linux/x86_64/..."
                parts = target.split("/linux/")
                if len(parts) > 1:
                    arch_part = parts[1].split("/")[0]
                    architectures.add(arch_part)
        return sorted(list(architectures))

    def _show_dry_run_info(
        self, catalog_name, catalog_version, architectures, eessi_data, sync_enabled
    ):
        """Show what would be done in dry run mode."""
        software_data = eessi_data.get("software", {})
        software_count = len(software_data)

        # Calculate total versions and targets
        total_versions = sum(
            len(pkg.get("versions", {})) for pkg in software_data.values()
        )
        total_targets = total_versions * len(architectures)

        self.stdout.write(self.style.SUCCESS("DRY RUN - No changes will be made"))
        self.stdout.write(
            f"Would create/update software catalog: {catalog_name} {catalog_version}"
        )
        self.stdout.write(f"Would process {software_count} software packages")
        self.stdout.write(f"Would process {total_versions} software versions")
        self.stdout.write(f"Would process {total_targets} software targets")
        self.stdout.write(f"Detected architectures: {', '.join(architectures)}")
        self.stdout.write(f"Detected version: {catalog_version}")

        if sync_enabled:
            self.stdout.write(
                self.style.WARNING(
                    "SYNC ENABLED: Missing packages/versions/targets will be DELETED"
                )
            )
        else:
            self.stdout.write("Sync disabled: Existing records will be preserved")

        # Check if catalog exists and show what would be removed
        try:
            from waldur_mastermind.marketplace.models import SoftwareCatalog

            existing_catalog = SoftwareCatalog.objects.get(
                name=catalog_name, version=catalog_version
            )
            if sync_enabled:
                existing_packages = set(
                    existing_catalog.packages.values_list("name", flat=True)
                )
                json_packages = set(software_data.keys())
                packages_to_remove = existing_packages - json_packages
                if packages_to_remove:
                    self.stdout.write(
                        f"Would remove {len(packages_to_remove)} packages not in JSON"
                    )
        except SoftwareCatalog.DoesNotExist:
            pass

        # Show sample of software packages
        if software_data:
            self.stdout.write("\nSample software packages:")
            for i, (name, info) in enumerate(list(software_data.items())[:5]):
                version_count = len(info.get("versions", {}))
                self.stdout.write(f"  - {name} ({version_count} versions)")
            if len(software_data) > 5:
                self.stdout.write(f"  ... and {len(software_data) - 5} more packages")
