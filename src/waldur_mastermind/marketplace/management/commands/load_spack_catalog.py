"""
Management command to load Spack software catalog data.

Uses the unified catalog loader to fetch and load Spack package data
from the repology.json format into the software catalog models.
"""

from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace.catalog_loaders.spack import SpackCatalogLoader


class Command(BaseCommand):
    help = "Load Spack software catalog data using the unified catalog loader"

    def add_arguments(self, parser):
        parser.add_argument(
            "--catalog-name",
            type=str,
            default="Spack",
            help="Name of the software catalog (default: Spack)",
        )
        parser.add_argument(
            "--catalog-version",
            type=str,
            default="auto",
            help="Spack catalog version (auto-detect if not provided)",
        )
        parser.add_argument(
            "--data-url",
            type=str,
            default="https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json",
            help="URL for Spack repology.json data",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            default=True,
            help="Update existing catalog data (default: true)",
        )

    def handle(self, *args, **options):
        catalog_name = options["catalog_name"]
        catalog_version = options["catalog_version"]
        data_url = options["data_url"]
        dry_run = options["dry_run"]
        update_existing = options["update_existing"]

        self.stdout.write(f"Loading Spack catalog: {catalog_name} {catalog_version}")

        try:
            # Create loader with provided options
            loader = SpackCatalogLoader(
                catalog_name=catalog_name,
                catalog_version=catalog_version,
                data_url=data_url,
            )

            # Load catalog
            stats = loader.load_catalog(
                update_existing=update_existing, dry_run=dry_run
            )

            # Display results
            if dry_run:
                self.stdout.write(self.style.SUCCESS("DRY RUN - No changes made"))
                self.stdout.write("Would create/update:")
                self.stdout.write(f"  Packages: {stats.get('packages_created', 0)}")
                self.stdout.write(f"  Versions: {stats.get('versions_created', 0)}")
                self.stdout.write(f"  Targets: {stats.get('targets_created', 0)}")
            else:
                self.stdout.write(
                    self.style.SUCCESS("Spack catalog loaded successfully")
                )
                self.stdout.write(
                    f"  Packages created: {stats.get('packages_created', 0)}"
                )
                self.stdout.write(
                    f"  Packages updated: {stats.get('packages_updated', 0)}"
                )
                self.stdout.write(
                    f"  Versions created: {stats.get('versions_created', 0)}"
                )
                self.stdout.write(
                    f"  Targets created: {stats.get('targets_created', 0)}"
                )

                self.stdout.write(
                    self.style.SUCCESS(
                        "\nTo associate this catalog with an offering, use the marketplace API"
                    )
                )

        except Exception as e:
            raise CommandError(f"Failed to load Spack catalog: {e}")
