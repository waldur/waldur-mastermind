"""
EESSI catalog loader for unified software catalog system.

Loads EESSI (European Environment for Scientific Software Installations)
catalog data from the new API format into the generic software catalog models.
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


def _get_eessi_version(version_info: dict) -> str | None:
    """Extract the EESSI version from a version entry's required_modules.

    Handles both old format (string list) and new format (dict list):
    - Old: ["EESSI/2023.06", ...] → "2023.06"
    - New: [{"module_name": "EESSI", "module_version": "2023.06"}, ...] → "2023.06"

    Returns None if no EESSI module is found.
    """
    required_modules = version_info.get("required_modules", [])
    if not required_modules:
        return None

    first_module = required_modules[0]

    if isinstance(first_module, str):
        # Old format: "EESSI/2023.06"
        if first_module.startswith("EESSI/"):
            return first_module.split("/", 1)[1]
    elif isinstance(first_module, dict):
        # New format: {"module_name": "EESSI", "module_version": "2023.06"}
        if first_module.get("module_name") == "EESSI":
            return first_module.get("module_version")

    return None


def _get_eessi_version_key(version_info: dict) -> str:
    """Return unique version key for EESSI builds.

    EESSI may expose multiple builds with the same upstream version but
    different module versions.
    """
    module = version_info.get("module", {})
    module_version = module.get("module_version", "")
    if module_version:
        return module_version
    return version_info["version"]


class EESSICatalogLoader(BaseCatalogLoader):
    """
    Loader for EESSI software catalogs.

    Supports the new EESSI API format with separate files for
    main software and extensions.
    """

    def __init__(
        self,
        catalog_name: str = "EESSI",
        catalog_version: str = "auto",
        api_base_url: str = "https://www.eessi.io/api_data/data/",
        include_extensions: bool = True,
    ):
        # Initialize base class first to get logger
        super().__init__(catalog_name, "temp_version", "binary_runtime")

        # Auto-detect version from API data
        if catalog_version == "auto":
            catalog_version = self._detect_latest_version(api_base_url)

        # Update the version after detection
        self.catalog_version = catalog_version
        self.api_base_url = api_base_url.rstrip("/")
        self.include_extensions = include_extensions

    def _detect_latest_version(self, api_base_url: str) -> str:
        """Detect latest EESSI version from API data."""
        try:
            response = requests.get(
                f"{api_base_url}/eessi_api_metadata_software.json", timeout=30
            )
            response.raise_for_status()
            data = response.json()

            # Get latest version from architecture map
            versions = list(data.get("architectures_map", {}).keys())
            return max(versions) if versions else "unknown"
        except Exception as e:
            self.logger.warning(f"Could not detect EESSI version: {e}")
            return "unknown"

    def fetch_catalog_data(self) -> CatalogData:
        """Fetch EESSI catalog data from API."""
        try:
            # Fetch main software data
            software_data = self._fetch_software_data()

            # Fetch extension data if requested
            extensions_data = {}
            if self.include_extensions:
                extensions_data = self._fetch_extensions_data()

            # Combine into catalog data
            catalog_data = self._build_catalog_data(software_data, extensions_data)

            return catalog_data

        except requests.RequestException as e:
            raise CatalogLoadError(f"Failed to fetch EESSI data: {e}") from e
        except json.JSONDecodeError as e:
            raise CatalogLoadError(f"Invalid JSON in EESSI data: {e}") from e
        except Exception as e:
            raise CatalogLoadError(f"Error processing EESSI data: {e}") from e

    def _fetch_software_data(self) -> dict:
        """Fetch main software data from EESSI API."""
        url = f"{self.api_base_url}/eessi_api_metadata_software.json"
        self.logger.info(f"Fetching EESSI software data from {url}")

        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def _fetch_extensions_data(self) -> dict[str, dict]:
        """Fetch extension data from EESSI API."""
        extensions = {}

        # Known extension types
        extension_types = ["python", "r", "perl", "ruby", "octave", "component"]

        for ext_type in extension_types:
            url = f"{self.api_base_url}/eessi_api_metadata_ext-{ext_type}.json"
            try:
                self.logger.info(f"Fetching EESSI {ext_type} extensions from {url}")
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                extensions[ext_type] = response.json()
            except requests.RequestException as e:
                self.logger.warning(f"Could not fetch {ext_type} extensions: {e}")
                continue

        return extensions

    def _build_catalog_data(
        self, software_data: dict, extensions_data: dict[str, dict]
    ) -> CatalogData:
        """Build unified catalog data from EESSI API responses."""

        # Extract metadata from software data
        timestamp = software_data.get("timestamp", datetime.now().isoformat())
        architectures_map = software_data.get("architectures_map", {})

        # Build catalog metadata
        catalog_metadata = {
            "timestamp": timestamp,
            "architectures_map": architectures_map,
            "gpu_architectures_map": software_data.get("gpu_architectures_map", {}),
            "category_details": software_data.get("category_details", {}),
            "api_base_url": self.api_base_url,
        }

        # Create catalog data structure
        catalog_data = CatalogData(
            name=self.catalog_name,
            version=self.catalog_version,
            catalog_type=self.catalog_type,
            source_url=self.api_base_url,
            description=f"European Environment for Scientific Software Installations {self.catalog_version}",
            metadata=catalog_metadata,
            packages={},
        )

        # Process main software packages
        main_software = software_data.get("software", {})
        total_main_packages = len(main_software)
        self.logger.info(f"Processing {total_main_packages} main software packages")
        self._log_memory_usage("before main package processing")

        for i, (package_name, package_info) in enumerate(main_software.items(), 1):
            if i % 100 == 0 or i == total_main_packages:
                self.logger.info(f"Processed {i}/{total_main_packages} main packages")
            package_with_versions = self._process_eessi_package(
                package_name, package_info, False
            )
            if package_with_versions is not None:
                catalog_data.packages[package_name] = package_with_versions

        self._log_memory_usage("after main package processing")

        # Process extension packages
        total_extensions = sum(
            len(ext_data.get("software", {})) for ext_data in extensions_data.values()
        )
        self.logger.info(
            f"Processing {total_extensions} extension packages across {len(extensions_data)} extension types"
        )
        self._log_memory_usage("before extension processing")

        processed_extensions = 0
        for ext_type, ext_data in extensions_data.items():
            ext_software = ext_data.get("software", {})
            ext_count = len(ext_software)
            self.logger.info(f"Processing {ext_count} {ext_type} extension packages")

            for package_name, package_info in ext_software.items():
                processed_extensions += 1
                if (
                    processed_extensions % 500 == 0
                    or processed_extensions == total_extensions
                ):
                    self.logger.info(
                        f"Processed {processed_extensions}/{total_extensions} extension packages"
                    )
                package_with_versions = self._process_eessi_package(
                    package_name, package_info, True
                )
                if package_with_versions is not None:
                    # Use unique key to avoid conflicts
                    catalog_data.packages[f"{ext_type}:{package_name}"] = (
                        package_with_versions
                    )

        self.logger.info(
            f"Completed processing all packages: {len(catalog_data.packages)} total packages"
        )
        return catalog_data

    def _process_eessi_package(
        self, package_name: str, package_info: dict, is_extension: bool
    ) -> PackageWithVersions | None:
        """Process a single EESSI package.

        Returns None if no versions match the catalog's EESSI version.
        """

        # Extract package metadata
        description = package_info.get("description", "")
        homepage = package_info.get("homepage", "")
        categories = package_info.get("categories", [])

        # Filter versions to only those matching this catalog's EESSI version
        versions_list = package_info.get("versions", [])
        filtered_versions = [
            v for v in versions_list if _get_eessi_version(v) == self.catalog_version
        ]

        if not filtered_versions:
            return None

        # Determine parent software for extensions (collect from ALL versions)
        parent_software_names = []
        if is_extension and filtered_versions:
            seen = set()
            for v in filtered_versions:
                name = v.get("parent_software", {}).get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    parent_software_names.append(name)

        package_data = PackageData(
            name=package_name,
            description=description,
            homepage=homepage,
            categories=categories,
            licenses=package_info.get("licenses", []),
            is_extension=is_extension,
            parent_software_names=parent_software_names,
        )

        # Process filtered versions
        if len(filtered_versions) > 50:  # Log packages with many versions
            self.logger.debug(
                f"Processing package {package_name} with {len(filtered_versions)} versions"
            )

        versions = {}
        for i, version_info in enumerate(filtered_versions):
            # Log progress for packages with many versions
            if len(filtered_versions) > 100 and (i + 1) % 50 == 0:
                self.logger.debug(
                    f"Package {package_name}: processed {i + 1}/{len(filtered_versions)} versions"
                )

            version_with_targets = self._process_eessi_version(version_info)
            version_key = _get_eessi_version_key(version_info)
            versions[version_key] = version_with_targets

        return PackageWithVersions(package_data=package_data, versions=versions)

    def _process_eessi_version(self, version_info: dict) -> VersionWithTargets:
        """Process a single EESSI version using the new API format."""

        # Get module dict (new format only)
        module = version_info.get("module", {})
        required_modules = version_info.get("required_modules", [])

        # Extract version metadata
        version_data = VersionData(
            version=version_info["version"],
            module_version=module.get("module_version", ""),
            dependencies=required_modules,
            metadata={
                "versionsuffix": version_info.get("versionsuffix", ""),
                "toolchain": version_info.get("toolchain", {}),
                "toolchain_families_compatibility": version_info.get(
                    "toolchain_families_compatibility", []
                ),
                "module": module,
                "required_modules": required_modules,
                "extensions": version_info.get("extensions", []),
                "license": version_info.get("license", []),
                "image": version_info.get("image", ""),
                "categories": version_info.get("categories", []),
                "identifier": version_info.get("identifier", ""),
            },
        )

        # Process targets (CPU architectures)
        targets = []
        cpu_architectures = version_info.get("cpu_arch", [])
        for arch in cpu_architectures:
            # Parse architecture string (e.g., "x86_64/zen3")
            if "/" in arch:
                cpu_family, cpu_microarch = arch.split("/", 1)
            else:
                cpu_family, cpu_microarch = arch, "generic"

            # Build target path
            location = f"/cvmfs/software.eessi.io/versions/{self.catalog_version}/software/linux/{arch}"

            gpu_arch_map = version_info.get("gpu_arch", {})
            gpu_archs = sorted(
                {arch_val for archs in gpu_arch_map.values() for arch_val in archs}
            )

            target_data = TargetData(
                target_type="cpu_architecture",
                target_name=cpu_family,
                target_subtype=cpu_microarch,
                location=location,
                metadata={
                    "full_arch": arch,
                    "gpu_arch": gpu_arch_map,
                },
                gpu_architectures=gpu_archs,
            )
            targets.append(target_data)

        return VersionWithTargets(version_data=version_data, targets=targets)
