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

        # Detect API format by checking first package
        main_software = software_data.get("software", {})
        if main_software:
            first_pkg = next(iter(main_software.values()), {})
            first_version = (first_pkg.get("versions") or [{}])[0]
            has_new_format = isinstance(first_version.get("module"), dict)
            self.logger.info(
                f"Detected EESSI API format: {'new (dict-based)' if has_new_format else 'old (string-based)'}"
            )

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
    ) -> PackageWithVersions:
        """Process a single EESSI package."""

        # Extract package metadata
        description = package_info.get("description", "")
        homepage = package_info.get("homepage", "")
        categories = package_info.get("categories", [])

        # Determine parent software for extensions
        parent_software_name = ""
        if is_extension and package_info.get("versions"):
            # versions is a list in EESSI data, get parent from first version
            versions_list = package_info["versions"]
            if versions_list and isinstance(versions_list, list):
                first_version_info = versions_list[0]
                parent_info = first_version_info.get("parent_software", {})
                parent_software_name = parent_info.get("name", "")

        package_data = PackageData(
            name=package_name,
            description=description,
            homepage=homepage,
            categories=categories,
            licenses=package_info.get("licenses", []),
            is_extension=is_extension,
            parent_software_name=parent_software_name,
        )

        # Process versions
        versions_list = package_info.get("versions", [])
        if len(versions_list) > 50:  # Log packages with many versions
            self.logger.debug(
                f"Processing package {package_name} with {len(versions_list)} versions"
            )

        versions = {}
        for i, version_info in enumerate(versions_list):
            # Log progress for packages with many versions
            if len(versions_list) > 100 and (i + 1) % 50 == 0:
                self.logger.debug(
                    f"Package {package_name}: processed {i + 1}/{len(versions_list)} versions"
                )

            version_with_targets = self._process_eessi_version(version_info)
            versions[version_info["version"]] = version_with_targets

        return PackageWithVersions(package_data=package_data, versions=versions)

    def _process_eessi_version(self, version_info: dict) -> VersionWithTargets:
        """Process a single EESSI version with support for both old and new API formats."""

        # Handle module field - support both old 'modulename' string and new 'module' dict
        module_data = version_info.get("module")
        if module_data and isinstance(module_data, dict):
            # New format: module is a dict
            module = module_data
        else:
            # Old format: modulename is a string, convert to dict
            modulename = version_info.get("modulename", "")
            if modulename and "/" in modulename:
                name, version = modulename.split("/", 1)
                module = {
                    "full_module_name": modulename,
                    "module_name": name,
                    "module_version": version,
                }
            else:
                module = {
                    "full_module_name": modulename,
                    "module_name": modulename,
                    "module_version": "",
                }

        # Handle required_modules - support both string list and dict list
        raw_required_modules = version_info.get("required_modules", [])
        required_modules = []
        for rm in raw_required_modules:
            if isinstance(rm, dict):
                # New format: already a dict
                required_modules.append(rm)
            elif isinstance(rm, str):
                # Old format: string like "GCCcore/12.2.0"
                if "/" in rm:
                    name, version = rm.split("/", 1)
                    required_modules.append(
                        {
                            "full_module_name": rm,
                            "module_name": name,
                            "module_version": version,
                        }
                    )
                else:
                    required_modules.append(
                        {
                            "full_module_name": rm,
                            "module_name": rm,
                            "module_version": "",
                        }
                    )

        # Extract version metadata with new fields
        version_data = VersionData(
            version=version_info["version"],
            dependencies=required_modules,  # Store structured deps in dependencies
            metadata={
                "versionsuffix": version_info.get("versionsuffix", ""),
                "toolchain": version_info.get("toolchain", {}),
                "toolchain_families_compatibility": version_info.get(
                    "toolchain_families_compatibility", []
                ),
                "module": module,  # New structured module field
                "modulename": version_info.get(
                    "modulename", ""
                ),  # Keep for backwards compat
                "required_modules": required_modules,  # Structured required_modules
                "extensions": version_info.get("extensions", []),  # NEW field
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

            target_data = TargetData(
                target_type="cpu_architecture",
                target_name=cpu_family,
                target_subtype=cpu_microarch,
                location=location,
                metadata={
                    "full_arch": arch,
                    "gpu_arch": version_info.get("gpu_arch", {}),
                },
            )
            targets.append(target_data)

        return VersionWithTargets(version_data=version_data, targets=targets)
