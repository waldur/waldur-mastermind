"""
Base catalog loader framework for unified software catalog system.

Provides abstract base classes and common functionality for loading
different types of software catalogs (EESSI, Spack, conda-forge, etc.).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psutil
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_mastermind.marketplace.models import (
    SoftwareCatalog,
    SoftwarePackage,
    SoftwareTarget,
    SoftwareVersion,
)

logger = logging.getLogger(__name__)


@dataclass
class PackageData:
    """Data class for package information during loading."""

    name: str
    description: str = ""
    homepage: str = ""
    categories: list[str] = None
    licenses: list[str] = None
    maintainers: list[str] = None
    is_extension: bool = False
    parent_software_names: list[str] = None

    def __post_init__(self):
        if self.categories is None:
            self.categories = []
        if self.licenses is None:
            self.licenses = []
        if self.maintainers is None:
            self.maintainers = []
        if self.parent_software_names is None:
            self.parent_software_names = []


@dataclass
class VersionData:
    """Data class for version information during loading."""

    version: str
    module_version: str = ""
    release_date: datetime | None = None
    dependencies: list[str] = None
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []
        if self.metadata is None:
            self.metadata = {}

    @property
    def storage_key(self) -> str:
        """Unique key for version dicts and sync operations."""
        return self.module_version or self.version


def get_version_storage_key(version: SoftwareVersion) -> str:
    """Return the storage key for an existing SoftwareVersion row."""
    return version.module_version or version.version


def fetch_versions_by_storage_keys(
    package: SoftwarePackage, version_keys: set[str] | list[str]
) -> dict[str, SoftwareVersion]:
    """Fetch package versions matching loader storage keys in a single query."""
    if not version_keys:
        return {}

    return {
        get_version_storage_key(version): version
        for version in SoftwareVersion.objects.filter(package=package).filter(
            Q(module_version__in=version_keys)
            | Q(module_version="", version__in=version_keys)
        )
    }


@dataclass
class TargetData:
    """Data class for target information during loading."""

    target_type: str
    target_name: str
    target_subtype: str = ""
    location: str = ""
    metadata: dict[str, Any] = None
    gpu_architectures: list[str] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.gpu_architectures is None:
            self.gpu_architectures = []
        # Ensure location is never None to avoid database constraint violations
        if self.location is None:
            self.location = ""


@dataclass
class CatalogData:
    """Complete catalog data structure."""

    name: str
    version: str
    catalog_type: str
    source_url: str = ""
    description: str = ""
    metadata: dict[str, Any] = None
    packages: dict[str, "PackageWithVersions"] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.packages is None:
            self.packages = {}


@dataclass
class PackageWithVersions:
    """Package data with its versions and targets."""

    package_data: PackageData
    versions: dict[str, "VersionWithTargets"] = None

    def __post_init__(self):
        if self.versions is None:
            self.versions = {}


@dataclass
class VersionWithTargets:
    """Version data with its targets."""

    version_data: VersionData
    targets: list[TargetData] = None

    def __post_init__(self):
        if self.targets is None:
            self.targets = []


class BaseCatalogLoader(ABC):
    """
    Abstract base class for catalog loaders.

    Defines the interface and common functionality for loading
    different types of software catalogs.
    """

    def __init__(self, catalog_name: str, catalog_version: str, catalog_type: str):
        self.catalog_name = catalog_name
        self.catalog_version = catalog_version
        self.catalog_type = catalog_type
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def _log_memory_usage(self, stage: str):
        """Log current memory usage for monitoring large dataset processing."""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024  # Convert to MB
            self.logger.info(f"Memory usage at {stage}: {memory_mb:.1f} MB")
        except Exception as e:
            self.logger.debug(f"Could not get memory usage: {e}")

    @abstractmethod
    def fetch_catalog_data(self) -> CatalogData:
        """
        Fetch raw catalog data from source.

        Returns:
            CatalogData: Complete catalog data structure

        Raises:
            CatalogLoadError: If data fetching fails
        """
        pass

    def load_catalog(
        self,
        update_existing: bool = True,
        dry_run: bool = False,
        catalog: "SoftwareCatalog | None" = None,
        sync: bool = False,
    ) -> dict[str, int]:
        """
        Load catalog data into database.

        Args:
            update_existing: Whether to update existing packages
            dry_run: If True, don't save changes to database
            catalog: Optional pre-fetched SoftwareCatalog instance.
                     When provided (task path), used directly — no DB lookup.
                     When None (management command path), looked up or created.
            sync: If True, delete DB records not present in the incoming data.
                  Useful for cleaning up stale versions/packages after filtering.

        Returns:
            Dict with statistics (packages_created, versions_created, etc.)
        """
        self.logger.info(f"Loading catalog {self.catalog_name} {self.catalog_version}")
        self._log_memory_usage("start")

        try:
            # Fetch catalog data
            catalog_data = self.fetch_catalog_data()
            self._log_memory_usage("after data fetch")

            # Load into database
            stats = self._load_to_database(
                catalog_data, update_existing, dry_run, catalog=catalog, sync=sync
            )
            self._log_memory_usage("after database load")

            if not dry_run:
                self.logger.info(f"Successfully loaded catalog: {stats}")
            else:
                self.logger.info(f"Dry run completed: {stats}")

            return stats

        except Exception as e:
            self.logger.error(f"Failed to load catalog {self.catalog_name}: {e}")
            raise CatalogLoadError(f"Catalog loading failed: {e}") from e

    def _load_to_database(
        self,
        catalog_data: CatalogData,
        update_existing: bool,
        dry_run: bool,
        catalog: "SoftwareCatalog | None" = None,
        sync: bool = False,
    ) -> dict[str, int]:
        """Load catalog data to database models.

        Args:
            catalog_data: Parsed catalog data structure.
            update_existing: Whether to update existing packages.
            dry_run: If True, don't save changes to database.
            catalog: Optional pre-fetched SoftwareCatalog instance.
                     When provided (task path), used directly.
                     When None (management command path), looked up or created.
            sync: If True, delete stale DB records not in incoming data.
        """
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
            stats["packages_created"] = len(catalog_data.packages)
            stats["versions_created"] = sum(
                len(pkg.versions) for pkg in catalog_data.packages.values()
            )
            stats["targets_created"] = sum(
                len(ver.targets)
                for pkg in catalog_data.packages.values()
                for ver in pkg.versions.values()
            )
            return stats

        with transaction.atomic():
            if catalog is not None:
                # Task path: catalog already resolved, update metadata fields
                catalog.version = catalog_data.version
                catalog.last_successful_update = timezone.now()
                if update_existing:
                    catalog.source_url = catalog_data.source_url
                    catalog.description = catalog_data.description
                    catalog.metadata = catalog_data.metadata
                catalog.save()
            else:
                # Management command path: look up or create.
                # Use filter().first() + create() instead of get_or_create to
                # avoid MultipleObjectsReturned when multiple versions exist
                # for the same name+catalog_type (PUHURI-PORTALS-EF7).
                catalog = (
                    SoftwareCatalog.objects.filter(
                        name=catalog_data.name,
                        catalog_type=catalog_data.catalog_type,
                    )
                    .order_by("-modified")
                    .first()
                )

                if catalog is None:
                    catalog = SoftwareCatalog.objects.create(
                        name=catalog_data.name,
                        catalog_type=catalog_data.catalog_type,
                        version=catalog_data.version,
                        source_url=catalog_data.source_url,
                        description=catalog_data.description,
                        metadata=catalog_data.metadata,
                        last_successful_update=timezone.now(),
                    )
                else:
                    catalog.version = catalog_data.version
                    catalog.last_successful_update = timezone.now()
                    if update_existing:
                        catalog.source_url = catalog_data.source_url
                        catalog.description = catalog_data.description
                        catalog.metadata = catalog_data.metadata
                    catalog.save()

            # Process packages
            stats.update(
                self._process_packages(
                    catalog, catalog_data.packages, update_existing, sync=sync
                )
            )

        return stats

    def _process_packages(
        self,
        catalog: SoftwareCatalog,
        packages_data: dict[str, PackageWithVersions],
        update_existing: bool,
        sync: bool = False,
    ) -> dict[str, int]:
        """Process packages and their versions/targets."""
        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "versions_deleted": 0,
            "targets_created": 0,
            "targets_deleted": 0,
            "packages_deleted": 0,
        }

        # Track parent packages for extensions
        parent_packages = {}

        # Count main and extension packages
        main_packages = [
            pkg for pkg in packages_data.values() if not pkg.package_data.is_extension
        ]
        extension_packages = [
            pkg for pkg in packages_data.values() if pkg.package_data.is_extension
        ]

        total_main = len(main_packages)
        total_extensions = len(extension_packages)
        self.logger.info(
            f"Starting database operations: {total_main} main packages, {total_extensions} extensions"
        )
        self._log_memory_usage("before main packages")

        # First pass: create main packages in batches
        self.logger.info("Processing main packages...")
        main_stats = self._process_main_packages_bulk(
            catalog, packages_data, update_existing, total_main, sync=sync
        )
        parent_packages.update(main_stats["parent_packages"])
        stats["packages_created"] += main_stats["packages_created"]
        stats["packages_updated"] += main_stats["packages_updated"]
        stats["versions_created"] += main_stats["versions_created"]
        stats["versions_deleted"] += main_stats.get("versions_deleted", 0)
        stats["targets_created"] += main_stats["targets_created"]
        stats["targets_deleted"] += main_stats.get("targets_deleted", 0)

        self.logger.info(
            f"Completed main packages. Created {stats['packages_created']} packages, {stats['versions_created']} versions, {stats['targets_created']} targets"
        )
        self._log_memory_usage("after main packages")

        # Second pass: create extension packages in batches
        if total_extensions > 0:
            self.logger.info("Processing extension packages...")
            self._log_memory_usage("before extensions")
            extension_stats = self._process_extensions_bulk(
                catalog,
                packages_data,
                parent_packages,
                update_existing,
                total_extensions,
                sync=sync,
            )
            stats["packages_created"] += extension_stats["packages_created"]
            stats["packages_updated"] += extension_stats["packages_updated"]
            stats["versions_created"] += extension_stats["versions_created"]
            stats["versions_deleted"] += extension_stats.get("versions_deleted", 0)
            stats["targets_created"] += extension_stats["targets_created"]
            stats["targets_deleted"] += extension_stats.get("targets_deleted", 0)
            self._log_memory_usage("after extensions")

        # Sync: delete packages not in incoming data
        if sync:
            incoming_package_names = {
                pkg.package_data.name for pkg in packages_data.values()
            }
            stale_packages = SoftwarePackage.objects.filter(catalog=catalog).exclude(
                name__in=incoming_package_names
            )
            stale_count = stale_packages.count()
            if stale_count > 0:
                self.logger.info(
                    f"Sync: deleting {stale_count} stale packages from catalog"
                )
                stale_packages.delete()
                stats["packages_deleted"] += stale_count

        return stats

    def _process_main_packages_bulk(
        self,
        catalog: SoftwareCatalog,
        packages_data: dict[str, PackageWithVersions],
        update_existing: bool,
        total_main: int,
        sync: bool = False,
    ) -> dict[str, any]:
        """Process main packages in optimized batches."""
        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "versions_deleted": 0,
            "targets_created": 0,
            "targets_deleted": 0,
            "parent_packages": {},
        }

        batch_size = 25  # Smaller batches for main packages as they have more versions
        main_batch = []
        processed_main = 0

        for package_name, package_with_versions in packages_data.items():
            package_data = package_with_versions.package_data

            if package_data.is_extension:
                continue  # Skip extensions

            processed_main += 1
            main_batch.append((package_name, package_with_versions))

            # Process batch when full or at end
            if len(main_batch) >= batch_size or processed_main == total_main:
                batch_stats = self._process_main_batch(
                    catalog, main_batch, update_existing, sync=sync
                )
                stats["packages_created"] += batch_stats["packages_created"]
                stats["packages_updated"] += batch_stats["packages_updated"]
                stats["versions_created"] += batch_stats["versions_created"]
                stats["versions_deleted"] += batch_stats.get("versions_deleted", 0)
                stats["targets_created"] += batch_stats["targets_created"]
                stats["targets_deleted"] += batch_stats.get("targets_deleted", 0)
                stats["parent_packages"].update(batch_stats["parent_packages"])

                self.logger.info(
                    f"Database: processed {processed_main}/{total_main} main packages"
                )
                main_batch.clear()

        return stats

    def _process_main_batch(
        self,
        catalog: SoftwareCatalog,
        main_batch: list,
        update_existing: bool,
        sync: bool = False,
    ) -> dict[str, any]:
        """Process a batch of main packages efficiently."""
        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "versions_deleted": 0,
            "targets_created": 0,
            "targets_deleted": 0,
            "parent_packages": {},
        }

        # Prepare bulk data
        packages_to_create = []
        package_names_in_batch = []

        for package_name, package_with_versions in main_batch:
            package_data = package_with_versions.package_data
            package_names_in_batch.append(package_name)
            packages_to_create.append(
                SoftwarePackage(
                    catalog=catalog,
                    name=package_data.name,
                    description=package_data.description,
                    homepage=package_data.homepage,
                    categories=package_data.categories,
                    licenses=package_data.licenses,
                    maintainers=package_data.maintainers,
                    is_extension=package_data.is_extension,
                )
            )

        # Check which packages already exist
        existing_packages = {
            pkg.name: pkg
            for pkg in SoftwarePackage.objects.filter(
                catalog=catalog, name__in=package_names_in_batch
            )
        }

        # Bulk create new packages
        new_packages = []
        for pkg in packages_to_create:
            if pkg.name not in existing_packages:
                new_packages.append(pkg)

        if new_packages:
            created_packages = SoftwarePackage.objects.bulk_create(
                new_packages, ignore_conflicts=True
            )
            stats["packages_created"] += len(created_packages)

        # Get all packages (existing + newly created) for version processing and parent tracking
        all_packages = {
            pkg.name: pkg
            for pkg in SoftwarePackage.objects.filter(
                catalog=catalog, name__in=package_names_in_batch
            )
        }

        # Update parent packages mapping and process versions
        for package_name, package_with_versions in main_batch:
            package = all_packages.get(package_name)
            if package:
                stats["parent_packages"][package_name] = package
                version_stats = self._process_versions_bulk(
                    package, package_with_versions.versions, sync=sync
                )
                stats["versions_created"] += version_stats["versions_created"]
                stats["versions_deleted"] += version_stats.get("versions_deleted", 0)
                stats["targets_created"] += version_stats["targets_created"]
                stats["targets_deleted"] += version_stats.get("targets_deleted", 0)

        return stats

    def _process_extensions_bulk(
        self,
        catalog: SoftwareCatalog,
        packages_data: dict[str, PackageWithVersions],
        parent_packages: dict[str, SoftwarePackage],
        update_existing: bool,
        total_extensions: int,
        sync: bool = False,
    ) -> dict[str, int]:
        """Process extension packages in optimized batches."""
        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "versions_deleted": 0,
            "targets_created": 0,
            "targets_deleted": 0,
        }

        batch_size = 50  # Process extensions in batches
        extension_batch = []
        processed_extensions = 0

        for package_name, package_with_versions in packages_data.items():
            package_data = package_with_versions.package_data

            if not package_data.is_extension:
                continue

            processed_extensions += 1
            extension_batch.append((package_name, package_with_versions))

            # Process batch when full or at end
            if (
                len(extension_batch) >= batch_size
                or processed_extensions == total_extensions
            ):
                batch_stats = self._process_extension_batch(
                    catalog,
                    extension_batch,
                    parent_packages,
                    update_existing,
                    sync=sync,
                )
                stats["packages_created"] += batch_stats["packages_created"]
                stats["packages_updated"] += batch_stats["packages_updated"]
                stats["versions_created"] += batch_stats["versions_created"]
                stats["versions_deleted"] += batch_stats.get("versions_deleted", 0)
                stats["targets_created"] += batch_stats["targets_created"]
                stats["targets_deleted"] += batch_stats.get("targets_deleted", 0)

                self.logger.info(
                    f"Database: processed {processed_extensions}/{total_extensions} extension packages"
                )
                extension_batch.clear()

        return stats

    def _process_extension_batch(
        self,
        catalog: SoftwareCatalog,
        extension_batch: list,
        parent_packages: dict[str, SoftwarePackage],
        update_existing: bool,
        sync: bool = False,
    ) -> dict[str, int]:
        """Process a batch of extension packages efficiently."""
        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "versions_deleted": 0,
            "targets_created": 0,
            "targets_deleted": 0,
        }

        # Prepare bulk data with validation
        packages_to_create = []
        package_names_in_batch = []
        valid_extensions = []

        # Collect all required parent package names for this batch
        required_parents = set()
        extension_parent_mapping = {}
        for package_name, package_with_versions in extension_batch:
            package_data = package_with_versions.package_data
            for parent_name in package_data.parent_software_names:
                required_parents.add(parent_name)

        # Find any missing parent packages in batch
        missing_parents = required_parents - set(parent_packages.keys())
        if missing_parents:
            # Try to find them in the database (might be from previous runs)
            additional_parents = {
                pkg.name: pkg
                for pkg in SoftwarePackage.objects.filter(
                    catalog=catalog, name__in=list(missing_parents), is_extension=False
                )
            }
            parent_packages.update(additional_parents)
            still_missing = missing_parents - set(additional_parents.keys())
            if still_missing:
                self.logger.warning(
                    f"Missing parent packages for batch: {list(still_missing)}"
                )

        for package_name, package_with_versions in extension_batch:
            package_data = package_with_versions.package_data
            # Use actual package name for DB operations, not the dict key
            # which may be prefixed (e.g. "component:adwaita-icon-theme")
            actual_name = package_data.name

            # Resolve parent packages for this extension
            parent_objs = []
            for parent_name in package_data.parent_software_names:
                parent_obj = parent_packages.get(parent_name)
                if parent_obj:
                    parent_objs.append(parent_obj)
                else:
                    self.logger.warning(
                        f"Parent package {parent_name} not found for extension {actual_name}"
                    )

            if not parent_objs:
                self.logger.warning(
                    f"No parent packages found for extension {actual_name}"
                )
                continue

            package_names_in_batch.append(actual_name)
            valid_extensions.append((actual_name, package_with_versions))
            extension_parent_mapping[actual_name] = parent_objs
            packages_to_create.append(
                SoftwarePackage(
                    catalog=catalog,
                    name=actual_name,
                    description=package_data.description,
                    homepage=package_data.homepage,
                    categories=package_data.categories,
                    licenses=package_data.licenses,
                    maintainers=package_data.maintainers,
                    is_extension=package_data.is_extension,
                )
            )

        if not package_names_in_batch:
            return stats  # No valid packages to process

        # Check which packages already exist
        existing_packages = {
            pkg.name: pkg
            for pkg in SoftwarePackage.objects.filter(
                catalog=catalog, name__in=package_names_in_batch
            )
        }

        # Bulk create new packages
        new_packages = []
        for pkg in packages_to_create:
            if pkg.name not in existing_packages:
                new_packages.append(pkg)

        if new_packages:
            created_packages = SoftwarePackage.objects.bulk_create(
                new_packages, ignore_conflicts=True
            )
            stats["packages_created"] += len(created_packages)

        # Get all packages (existing + newly created) for version processing
        all_packages = {
            pkg.name: pkg
            for pkg in SoftwarePackage.objects.filter(
                catalog=catalog, name__in=package_names_in_batch
            )
        }

        # Set M2M parent relationships and process versions
        for actual_name, package_with_versions in valid_extensions:
            package = all_packages.get(actual_name)
            if package:
                parent_objs = extension_parent_mapping.get(actual_name, [])
                if parent_objs:
                    package.parent_softwares.set(parent_objs)
                version_stats = self._process_versions_bulk(
                    package, package_with_versions.versions, sync=sync
                )
                stats["versions_created"] += version_stats["versions_created"]
                stats["versions_deleted"] += version_stats.get("versions_deleted", 0)
                stats["targets_created"] += version_stats["targets_created"]
                stats["targets_deleted"] += version_stats.get("targets_deleted", 0)

        return stats

    def _process_versions_bulk(
        self,
        package: SoftwarePackage,
        versions_data: dict[str, VersionWithTargets],
        sync: bool = False,
    ) -> dict[str, int]:
        """Process versions and targets with bulk operations."""
        stats = {
            "versions_created": 0,
            "versions_deleted": 0,
            "targets_created": 0,
            "targets_deleted": 0,
        }

        if not versions_data:
            return stats

        # Prepare bulk data
        versions_to_create = []
        version_key_set = set(versions_data.keys())

        all_versions = fetch_versions_by_storage_keys(package, version_key_set)

        # Prepare new versions for bulk create
        for version_key, version_with_targets in versions_data.items():
            if version_key not in all_versions:
                version_data = version_with_targets.version_data
                versions_to_create.append(
                    SoftwareVersion(
                        package=package,
                        version=version_data.version,
                        module_version=version_data.module_version,
                        release_date=version_data.release_date,
                        dependencies=version_data.dependencies,
                        metadata=version_data.metadata,
                    )
                )

        # Bulk create versions
        if versions_to_create:
            created_versions = SoftwareVersion.objects.bulk_create(
                versions_to_create, ignore_conflicts=True
            )
            stats["versions_created"] += len(created_versions)
            missing_keys = {
                version_key
                for version_key in version_key_set
                if version_key not in all_versions
            }
            if missing_keys:
                all_versions.update(
                    fetch_versions_by_storage_keys(package, missing_keys)
                )

        # Bulk process targets
        targets_to_create = []
        for version_key, version_with_targets in versions_data.items():
            version_obj = all_versions.get(version_key)
            if version_obj:
                for target_data in version_with_targets.targets:
                    # Ensure location is not None (database constraint)
                    location = target_data.location or ""
                    targets_to_create.append(
                        SoftwareTarget(
                            version=version_obj,
                            target_type=target_data.target_type,
                            target_name=target_data.target_name,
                            target_subtype=target_data.target_subtype,
                            location=location,
                            metadata=target_data.metadata,
                            gpu_architectures=target_data.gpu_architectures,
                        )
                    )

        # Bulk create targets
        if targets_to_create:
            created_targets = SoftwareTarget.objects.bulk_create(
                targets_to_create, ignore_conflicts=True
            )
            stats["targets_created"] += len(created_targets)

        # Sync: delete versions not in incoming data
        if sync:
            stale_count, _ = (
                SoftwareVersion.objects.filter(package=package)
                .exclude(
                    Q(module_version__in=version_key_set)
                    | Q(module_version="", version__in=version_key_set)
                )
                .delete()
            )
            stats["versions_deleted"] += stale_count

        return stats

    def _create_or_update_package(
        self,
        catalog: SoftwareCatalog,
        package_data: PackageData,
        parent_packages: list[SoftwarePackage] | None,
        update_existing: bool,
    ) -> tuple[SoftwarePackage, bool]:
        """Create or update a software package."""
        defaults = {
            "description": package_data.description,
            "homepage": package_data.homepage,
            "categories": package_data.categories,
            "licenses": package_data.licenses,
            "maintainers": package_data.maintainers,
            "is_extension": package_data.is_extension,
        }

        package, created = SoftwarePackage.objects.get_or_create(
            catalog=catalog, name=package_data.name, defaults=defaults
        )

        if not created and update_existing:
            for field, value in defaults.items():
                setattr(package, field, value)
            package.save()

        if parent_packages:
            package.parent_softwares.set(parent_packages)

        return package, created


class CatalogLoadError(Exception):
    """Exception raised when catalog loading fails."""

    pass
