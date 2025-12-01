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

from django.db import transaction
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
    parent_software_name: str = ""

    def __post_init__(self):
        if self.categories is None:
            self.categories = []
        if self.licenses is None:
            self.licenses = []
        if self.maintainers is None:
            self.maintainers = []


@dataclass
class VersionData:
    """Data class for version information during loading."""

    version: str
    release_date: datetime | None = None
    dependencies: list[str] = None
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []
        if self.metadata is None:
            self.metadata = {}


@dataclass
class TargetData:
    """Data class for target information during loading."""

    target_type: str
    target_name: str
    target_subtype: str = ""
    location: str = ""
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


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
        self, update_existing: bool = True, dry_run: bool = False
    ) -> dict[str, int]:
        """
        Load catalog data into database.

        Args:
            update_existing: Whether to update existing packages
            dry_run: If True, don't save changes to database

        Returns:
            Dict with statistics (packages_created, versions_created, etc.)
        """
        self.logger.info(f"Loading catalog {self.catalog_name} {self.catalog_version}")

        try:
            # Fetch catalog data
            catalog_data = self.fetch_catalog_data()

            # Load into database
            stats = self._load_to_database(catalog_data, update_existing, dry_run)

            if not dry_run:
                self.logger.info(f"Successfully loaded catalog: {stats}")
            else:
                self.logger.info(f"Dry run completed: {stats}")

            return stats

        except Exception as e:
            self.logger.error(f"Failed to load catalog {self.catalog_name}: {e}")
            raise CatalogLoadError(f"Catalog loading failed: {e}") from e

    def _load_to_database(
        self, catalog_data: CatalogData, update_existing: bool, dry_run: bool
    ) -> dict[str, int]:
        """Load catalog data to database models."""
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
            # Create or update catalog
            catalog, created = SoftwareCatalog.objects.get_or_create(
                name=catalog_data.name,
                version=catalog_data.version,
                catalog_type=catalog_data.catalog_type,
                defaults={
                    "source_url": catalog_data.source_url,
                    "description": catalog_data.description,
                    "metadata": catalog_data.metadata,
                    "last_successful_update": timezone.now(),
                },
            )

            if not created and update_existing:
                catalog.source_url = catalog_data.source_url
                catalog.description = catalog_data.description
                catalog.metadata = catalog_data.metadata
                catalog.last_successful_update = timezone.now()
                catalog.save()

            # Process packages
            stats.update(
                self._process_packages(catalog, catalog_data.packages, update_existing)
            )

        return stats

    def _process_packages(
        self,
        catalog: SoftwareCatalog,
        packages_data: dict[str, PackageWithVersions],
        update_existing: bool,
    ) -> dict[str, int]:
        """Process packages and their versions/targets."""
        stats = {
            "packages_created": 0,
            "packages_updated": 0,
            "versions_created": 0,
            "targets_created": 0,
        }

        # Track parent packages for extensions
        parent_packages = {}

        # First pass: create main packages
        for package_name, package_with_versions in packages_data.items():
            package_data = package_with_versions.package_data

            if package_data.is_extension:
                continue  # Process extensions in second pass

            package, created = self._create_or_update_package(
                catalog, package_data, None, update_existing
            )
            parent_packages[package_name] = package

            if created:
                stats["packages_created"] += 1
            elif update_existing:
                stats["packages_updated"] += 1

            # Process versions
            version_stats = self._process_versions(
                package, package_with_versions.versions
            )
            stats["versions_created"] += version_stats["versions_created"]
            stats["targets_created"] += version_stats["targets_created"]

        # Second pass: create extension packages
        for package_name, package_with_versions in packages_data.items():
            package_data = package_with_versions.package_data

            if not package_data.is_extension:
                continue

            parent_package = parent_packages.get(package_data.parent_software_name)
            if not parent_package:
                self.logger.warning(
                    f"Parent package {package_data.parent_software_name} not found for extension {package_name}"
                )
                continue

            package, created = self._create_or_update_package(
                catalog, package_data, parent_package, update_existing
            )

            if created:
                stats["packages_created"] += 1
            elif update_existing:
                stats["packages_updated"] += 1

            # Process versions
            version_stats = self._process_versions(
                package, package_with_versions.versions
            )
            stats["versions_created"] += version_stats["versions_created"]
            stats["targets_created"] += version_stats["targets_created"]

        return stats

    def _create_or_update_package(
        self,
        catalog: SoftwareCatalog,
        package_data: PackageData,
        parent_package: SoftwarePackage | None,
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
            "parent_software": parent_package,
        }

        package, created = SoftwarePackage.objects.get_or_create(
            catalog=catalog, name=package_data.name, defaults=defaults
        )

        if not created and update_existing:
            for field, value in defaults.items():
                setattr(package, field, value)
            package.save()

        return package, created

    def _process_versions(
        self, package: SoftwarePackage, versions_data: dict[str, VersionWithTargets]
    ) -> dict[str, int]:
        """Process versions and their targets for a package."""
        stats = {"versions_created": 0, "targets_created": 0}

        for version_name, version_with_targets in versions_data.items():
            version_data = version_with_targets.version_data

            version, created = SoftwareVersion.objects.get_or_create(
                package=package,
                version=version_data.version,
                defaults={
                    "release_date": version_data.release_date,
                    "dependencies": version_data.dependencies,
                    "metadata": version_data.metadata,
                },
            )

            if created:
                stats["versions_created"] += 1

            # Process targets
            for target_data in version_with_targets.targets:
                target, target_created = SoftwareTarget.objects.get_or_create(
                    version=version,
                    target_type=target_data.target_type,
                    target_name=target_data.target_name,
                    target_subtype=target_data.target_subtype,
                    defaults={
                        "location": target_data.location,
                        "metadata": target_data.metadata,
                    },
                )

                if target_created:
                    stats["targets_created"] += 1

        return stats


class CatalogLoadError(Exception):
    """Exception raised when catalog loading fails."""

    pass
