"""
Spack catalog loader for unified software catalog system.

Loads Spack package data from the repology.json format into
the generic software catalog models.
"""

import json
from datetime import datetime

import requests

from .base import (
    BaseCatalogLoader,
    CatalogData,
    CatalogLoadError,
    PackageData,
    PackageWithVersions,
    TargetData,
    VersionData,
    VersionWithTargets,
)


class SpackCatalogLoader(BaseCatalogLoader):
    """
    Loader for Spack software catalogs.

    Supports the Spack repology.json format from packages.spack.io.
    """

    def __init__(
        self,
        catalog_name: str = "Spack",
        catalog_version: str = "auto",
        data_url: str = "https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json",
    ):
        # Initialize base class first to get logger
        super().__init__(catalog_name, "temp_version", "source_package")

        # Auto-detect version from data timestamp
        if catalog_version == "auto":
            catalog_version = self._detect_version_from_timestamp(data_url)

        # Update the version after detection
        self.catalog_version = catalog_version
        self.data_url = data_url

    def _detect_version_from_timestamp(self, data_url: str) -> str:
        """Detect Spack version based on data timestamp."""
        try:
            response = requests.get(data_url, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Use last_update timestamp to create version
            last_update = data.get(
                "last_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
            date_part = last_update.split()[0]
            return date_part.replace("-", ".")
        except Exception as e:
            self.logger.warning(f"Could not detect Spack version: {e}")
            return "latest"

    def fetch_catalog_data(self) -> CatalogData:
        """Fetch Spack catalog data from repology.json."""
        try:
            # Fetch Spack data
            self.logger.info(f"Fetching Spack data from {self.data_url}")
            response = requests.get(self.data_url, timeout=60)
            response.raise_for_status()
            spack_data = response.json()

            # Build catalog data
            catalog_data = self._build_catalog_data(spack_data)

            return catalog_data

        except requests.RequestException as e:
            raise CatalogLoadError(f"Failed to fetch Spack data: {e}") from e
        except json.JSONDecodeError as e:
            raise CatalogLoadError(f"Invalid JSON in Spack data: {e}") from e
        except Exception as e:
            raise CatalogLoadError(f"Error processing Spack data: {e}") from e

    def _build_catalog_data(self, spack_data: dict) -> CatalogData:
        """Build unified catalog data from Spack repology response."""

        # Extract metadata
        last_update = spack_data.get("last_update", datetime.now().isoformat())
        num_packages = spack_data.get("num_packages", 0)

        # Build catalog metadata
        catalog_metadata = {
            "last_update": last_update,
            "num_packages": num_packages,
            "data_source": "spack/packages.spack.io",
            "format": "repology.json",
        }

        # Create catalog data structure
        catalog_data = CatalogData(
            name=self.catalog_name,
            version=self.catalog_version,
            catalog_type=self.catalog_type,
            source_url=self.data_url,
            description=f"Spack package manager catalog ({num_packages} packages)",
            metadata=catalog_metadata,
            packages={},
        )

        # Process packages
        packages = spack_data.get("packages", {})
        for package_name, package_info in packages.items():
            package_with_versions = self._process_spack_package(
                package_name, package_info
            )
            catalog_data.packages[package_name] = package_with_versions

        return catalog_data

    def _process_spack_package(
        self, package_name: str, package_info: dict
    ) -> PackageWithVersions:
        """Process a single Spack package."""

        # Extract package metadata
        description = package_info.get("summary", "")
        homepage = (
            package_info.get("homepages", [""])[0]
            if package_info.get("homepages")
            else ""
        )
        categories = package_info.get("categories", [])
        licenses = package_info.get("licenses", [])
        maintainers = package_info.get("maintainers", [])
        package_info.get("alias", [])

        package_data = PackageData(
            name=package_name,
            description=description,
            homepage=homepage,
            categories=categories,
            licenses=licenses,
            maintainers=maintainers,
            is_extension=False,  # Spack doesn't have extension hierarchy like EESSI
        )

        # Process versions
        versions = {}
        version_list = package_info.get("version", [])

        # Handle single version or version list
        if isinstance(version_list, dict):
            version_list = [version_list]

        for version_info in version_list:
            version_with_targets = self._process_spack_version(
                package_name, version_info, package_info
            )
            version_name = version_info.get("version", "unknown")
            versions[version_name] = version_with_targets

        return PackageWithVersions(package_data=package_data, versions=versions)

    def _process_spack_version(
        self, package_name: str, version_info: dict, package_info: dict
    ) -> VersionWithTargets:
        """Process a single Spack version."""

        # Extract version metadata
        version_name = version_info.get("version", "unknown")
        downloads = version_info.get("downloads", [])
        branch = version_info.get("branch", "")

        # Build version metadata
        version_metadata = {
            "downloads": downloads,
            "branch": branch,
            "dependencies": package_info.get("dependencies", []),
            "patches": package_info.get("patches", []),
            "aliases": package_info.get("alias", []),
            "package_downloads": package_info.get("downloads", []),
        }

        version_data = VersionData(
            version=version_name,
            dependencies=package_info.get("dependencies", []),
            metadata=version_metadata,
        )

        # Create generic targets for Spack (build variants/platforms)
        targets = self._create_spack_targets(version_name, version_info, package_info)

        return VersionWithTargets(version_data=version_data, targets=targets)

    def _create_spack_targets(
        self, version_name: str, version_info: dict, package_info: dict
    ) -> list[TargetData]:
        """Create targets for Spack package (build variants, platforms, etc.)."""
        targets = []

        # Create default build target
        downloads = version_info.get("downloads", [])
        primary_download = downloads[0] if downloads else ""

        default_target = TargetData(
            target_type="build_variant",
            target_name="default",
            location=primary_download,
            metadata={
                "downloads": downloads,
                "dependencies": package_info.get("dependencies", []),
                "patches": package_info.get("patches", []),
                "categories": package_info.get("categories", []),
                "version_info": version_info,
            },
        )
        targets.append(default_target)

        # Create platform targets based on categories
        categories = package_info.get("categories", [])

        # Platform-specific targets
        if "windows" in categories:
            windows_target = TargetData(
                target_type="platform",
                target_name="windows",
                metadata={"supported": True},
            )
            targets.append(windows_target)

        # Detectable packages (externally provided)
        if "detectable" in categories:
            external_target = TargetData(
                target_type="external",
                target_name="system",
                metadata={"detectable": True},
            )
            targets.append(external_target)

        # Build tools
        if "build-tools" in categories:
            build_target = TargetData(
                target_type="build_system",
                target_name="build-tool",
                metadata={"category": "build-tools"},
            )
            targets.append(build_target)

        return targets
