import json
import os

from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace.catalog_loaders.eessi import EESSICatalogLoader


class Command(BaseCommand):
    help = "Load EESSI software catalog data using the unified catalog loader"

    def add_arguments(self, parser):
        parser.add_argument(
            "--json-file",
            type=str,
            help="Path to JSON file containing EESSI catalog data",
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
            default="auto",
            help="EESSI catalog version (auto-detect if not provided)",
        )
        parser.add_argument(
            "--api-url",
            type=str,
            default="https://www.eessi.io/api_data/data/",
            help="Base URL for EESSI API data",
        )
        parser.add_argument(
            "--include-extensions",
            action="store_true",
            default=True,
            help="Include extension packages (Python, R packages, etc.)",
        )
        parser.add_argument(
            "--no-extensions",
            action="store_true",
            help="Exclude extension packages",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            help="Update existing catalog data",
        )
        parser.add_argument(
            "--no-sync",
            action="store_true",
            help="Preserve existing records not in source data",
        )

    def handle(self, *args, **options):
        catalog_name = options["catalog_name"]
        catalog_version = options["catalog_version"]
        json_file = options.get("json_file")
        api_url = options["api_url"]
        include_extensions = (
            options["include_extensions"] and not options["no_extensions"]
        )
        dry_run = options["dry_run"]
        update_existing = options["update_existing"]
        no_sync = options["no_sync"]

        self.stdout.write(f"Loading EESSI catalog: {catalog_name} {catalog_version}")

        try:
            if json_file:
                # Load from JSON file using legacy format support
                if not os.path.exists(json_file):
                    raise CommandError(f"JSON file not found: {json_file}")

                with open(json_file) as f:
                    json_data = json.load(f)

                # Process legacy format
                stats = self._load_from_json(
                    json_data,
                    catalog_name,
                    catalog_version,
                    dry_run,
                    update_existing,
                    no_sync,
                )
            else:
                # Create loader with provided options for API loading
                loader = EESSICatalogLoader(
                    catalog_name=catalog_name,
                    catalog_version=catalog_version,
                    api_base_url=api_url,
                    include_extensions=include_extensions,
                )

                # Load catalog
                stats = loader.load_catalog(
                    update_existing=update_existing, dry_run=dry_run
                )

            # Display results
            if dry_run:
                self._display_dry_run_results(
                    stats, json_data if json_file else None, no_sync
                )
            else:
                self._display_success_results(stats)

        except Exception as e:
            raise CommandError(f"Failed to load EESSI catalog: {e}")

    def _load_from_json(
        self,
        json_data,
        catalog_name,
        catalog_version,
        dry_run,
        update_existing,
        no_sync,
    ):
        """Load catalog from legacy JSON format."""
        import re

        from django.db import transaction
        from django.utils import timezone

        from waldur_mastermind.marketplace.models import (
            SoftwareCatalog,
            SoftwarePackage,
            SoftwareTarget,
            SoftwareVersion,
        )

        # Extract version from targets if auto
        if catalog_version == "auto":
            targets = json_data.get("targets", [])
            if targets:
                # Extract version from path like /cvmfs/software.eessi.io/versions/2023.06/...
                version_match = re.search(r"/versions/([^/]+)/", targets[0])
                if version_match:
                    catalog_version = version_match.group(1)

        # Extract architectures from targets
        architectures = []
        for target in json_data.get("targets", []):
            arch_match = re.search(r"/linux/([^/]+)/", target)
            if arch_match:
                arch = arch_match.group(1)
                if arch not in architectures:
                    architectures.append(arch)

        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "targets_created": 0,
            "packages_deleted": 0,
            "versions_deleted": 0,
            "targets_deleted": 0,
        }

        if dry_run:
            # Calculate what would be done
            software_packages = json_data.get("software", {})
            total_versions = sum(
                len(pkg.get("versions", {})) for pkg in software_packages.values()
            )

            stats["packages_created"] = len(software_packages)
            stats["versions_created"] = total_versions
            stats["targets_created"] = total_versions * len(architectures)

            # Check for existing packages that would be removed
            if not no_sync:
                try:
                    catalog = SoftwareCatalog.objects.get(
                        name=catalog_name, version=catalog_version
                    )
                    existing_packages = list(
                        SoftwarePackage.objects.filter(catalog=catalog).values_list(
                            "name", flat=True
                        )
                    )
                    json_packages = list(software_packages.keys())
                    to_remove = [
                        pkg for pkg in existing_packages if pkg not in json_packages
                    ]
                    stats["packages_to_remove"] = len(to_remove)
                except SoftwareCatalog.DoesNotExist:
                    stats["packages_to_remove"] = 0

            return stats

        with transaction.atomic():
            # Create or get catalog
            catalog, catalog_created = SoftwareCatalog.objects.get_or_create(
                name=catalog_name,
                version=catalog_version,
                defaults={
                    "catalog_type": "binary_runtime",
                    "source_url": "https://software.eessi.io/",
                    "description": f"European Environment for Scientific Software Installations {catalog_version}",
                    "metadata": {"architectures": architectures},
                    "last_successful_update": timezone.now(),
                },
            )

            if not catalog_created and not update_existing:
                self.stdout.write(
                    "Software catalog already exists. Use --update-existing to update it."
                )
                return stats

            if not catalog_created and update_existing:
                catalog.source_url = "https://software.eessi.io/"
                catalog.description = f"European Environment for Scientific Software Installations {catalog_version}"
                catalog.metadata = {"architectures": architectures}
                catalog.last_successful_update = timezone.now()
                catalog.save()
                self.stdout.write("Updated existing software catalog")

            # Process software packages
            software_packages = json_data.get("software", {})
            json_package_names = set(software_packages.keys())

            for package_name, package_info in software_packages.items():
                package, package_created = SoftwarePackage.objects.get_or_create(
                    catalog=catalog,
                    name=package_name,
                    defaults={
                        "description": package_info.get("description", ""),
                        "homepage": package_info.get("homepage", ""),
                        "categories": package_info.get("categories", []),
                        "licenses": package_info.get("licenses", []),
                        "maintainers": package_info.get("maintainers", []),
                        "is_extension": False,
                    },
                )

                if package_created:
                    stats["packages_created"] += 1
                elif update_existing:
                    package.description = package_info.get("description", "")
                    package.homepage = package_info.get("homepage", "")
                    package.save()
                    stats["packages_updated"] += 1

                # Process versions
                for version_name, version_info in package_info.get(
                    "versions", {}
                ).items():
                    module = version_info.get("module", {})
                    module_version = module.get("module_version", "")
                    version, version_created = SoftwareVersion.objects.get_or_create(
                        package=package,
                        version=version_name,
                        module_version=module_version,
                        defaults={
                            "dependencies": [],
                            "metadata": version_info,
                        },
                    )

                    if version_created:
                        stats["versions_created"] += 1

                    # Create targets for each architecture
                    for target_path in json_data.get("targets", []):
                        arch_match = re.search(r"/linux/([^/]+)/", target_path)
                        if arch_match:
                            arch = arch_match.group(1)
                            target, target_created = (
                                SoftwareTarget.objects.get_or_create(
                                    version=version,
                                    target_type="cpu_architecture",
                                    target_name=arch,
                                    target_subtype="generic",
                                    defaults={
                                        "location": target_path,
                                        "metadata": {"full_arch": arch},
                                    },
                                )
                            )

                            if target_created:
                                stats["targets_created"] += 1

            # Handle sync - remove packages not in JSON
            if not no_sync:
                existing_packages = SoftwarePackage.objects.filter(catalog=catalog)
                for package in existing_packages:
                    if package.name not in json_package_names:
                        package.delete()
                        stats["packages_deleted"] += 1

        return stats

    def _display_dry_run_results(self, stats, json_data, no_sync):
        """Display dry run results."""
        self.stdout.write(self.style.SUCCESS("DRY RUN - No changes will be made"))

        if json_data:
            # Extract info for display
            software_packages = json_data.get("software", {})
            catalog_version = "auto-detected"

            # Try to extract version from targets
            targets = json_data.get("targets", [])
            if targets:
                import re

                version_match = re.search(r"/versions/([^/]+)/", targets[0])
                if version_match:
                    catalog_version = version_match.group(1)

            # Extract architectures
            architectures = []
            for target in targets:
                arch_match = re.search(r"/linux/([^/]+)/", target)
                if arch_match:
                    arch = arch_match.group(1)
                    if arch not in architectures:
                        architectures.append(arch)

            self.stdout.write(
                f"Would create/update software catalog: EESSI {catalog_version}"
            )
            self.stdout.write(
                f"Would process {len(software_packages)} software packages"
            )
            self.stdout.write(
                f"Would process {stats.get('versions_created', 0)} software versions"
            )
            self.stdout.write(
                f"Would process {stats.get('targets_created', 0)} software targets"
            )

            if architectures:
                self.stdout.write(
                    f"Detected architectures: {', '.join(sorted(architectures))}"
                )

            if no_sync:
                self.stdout.write("Sync disabled: Existing records will be preserved")
            else:
                self.stdout.write(
                    "SYNC ENABLED: Missing packages/versions/targets will be DELETED"
                )
                if stats.get("packages_to_remove", 0) > 0:
                    self.stdout.write(
                        f"Would remove {stats['packages_to_remove']} packages not in JSON"
                    )

            # Show sample packages
            self.stdout.write("\nSample software packages:")
            for name, info in list(software_packages.items())[:5]:
                version_count = len(info.get("versions", {}))
                self.stdout.write(f"  - {name} ({version_count} versions)")
        else:
            self.stdout.write("Would create/update:")
            self.stdout.write(f"  Packages: {stats.get('packages_created', 0)}")
            self.stdout.write(f"  Versions: {stats.get('versions_created', 0)}")
            self.stdout.write(f"  Targets: {stats.get('targets_created', 0)}")

    def _display_success_results(self, stats):
        """Display success results."""
        self.stdout.write(self.style.SUCCESS("EESSI catalog loaded successfully"))
        self.stdout.write(f"  Packages created: {stats.get('packages_created', 0)}")
        self.stdout.write(f"  Packages updated: {stats.get('packages_updated', 0)}")
        self.stdout.write(f"  Versions created: {stats.get('versions_created', 0)}")
        self.stdout.write(f"  Targets created: {stats.get('targets_created', 0)}")

        if stats.get("packages_deleted", 0) > 0:
            self.stdout.write(f"  Packages deleted: {stats['packages_deleted']}")

        self.stdout.write(
            self.style.SUCCESS(
                "\nTo associate this catalog with an offering, use the marketplace API"
            )
        )
