"""
Test cases for software catalog loaders.

Tests the EESSI and Spack catalog loaders using real data snapshots
to ensure accurate parsing and database loading.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_mastermind.marketplace.catalog_loaders.base import CatalogLoadError
from waldur_mastermind.marketplace.catalog_loaders.eessi import EESSICatalogLoader
from waldur_mastermind.marketplace.catalog_loaders.spack import SpackCatalogLoader
from waldur_mastermind.marketplace.models import (
    SoftwareCatalog,
    SoftwarePackage,
    SoftwareTarget,
    SoftwareVersion,
)


class BaseLoaderTestCase(TestCase):
    """Base test case with common fixture loading utilities."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixtures_dir = Path(__file__).parent / "fixtures" / "catalog_data"

    def load_test_fixture(self, filename):
        """Load test fixture JSON data."""
        fixture_path = self.fixtures_dir / filename
        if not fixture_path.exists():
            self.skipTest(f"Test fixture {filename} not found")

        with open(fixture_path) as f:
            return json.load(f)


class EESSICatalogLoaderTest(BaseLoaderTestCase):
    """Test cases for EESSI catalog loader."""

    def setUp(self):
        self.eessi_software_data = self.load_test_fixture("eessi_software_test.json")
        self.eessi_extensions_data = self.load_test_fixture(
            "eessi_extensions_python_test.json"
        )

    def test_loader_initialization(self):
        """Test EESSI loader can be initialized with various parameters."""
        # Test with defaults
        loader = EESSICatalogLoader()
        self.assertEqual(loader.catalog_name, "EESSI")
        self.assertEqual(loader.catalog_type, "binary_runtime")

        # Test with custom parameters
        loader = EESSICatalogLoader(
            catalog_name="Custom EESSI",
            catalog_version="2023.06",
            include_extensions=False,
        )
        self.assertEqual(loader.catalog_name, "Custom EESSI")
        self.assertEqual(loader.catalog_version, "2023.06")
        self.assertFalse(loader.include_extensions)

    @patch("requests.get")
    def test_version_detection(self, mock_get):
        """Test automatic version detection from API data."""
        mock_response = Mock()
        mock_response.json.return_value = self.eessi_software_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        loader = EESSICatalogLoader(catalog_version="auto")
        # Should detect "2025.06" as latest version from architectures_map
        self.assertEqual(loader.catalog_version, "2025.06")

    @patch("requests.get")
    def test_software_data_fetching(self, mock_get):
        """Test fetching main software data from EESSI API."""
        mock_response = Mock()
        mock_response.json.return_value = self.eessi_software_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        loader = EESSICatalogLoader()
        software_data = loader._fetch_software_data()

        self.assertIn("timestamp", software_data)
        self.assertIn("software", software_data)
        self.assertIn("architectures_map", software_data)

    @patch("requests.get")
    def test_extensions_data_fetching(self, mock_get):
        """Test fetching extension data from EESSI API."""
        # Mock successful extension fetch
        mock_response = Mock()
        mock_response.json.return_value = self.eessi_extensions_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        loader = EESSICatalogLoader()
        extensions_data = loader._fetch_extensions_data()

        self.assertIsInstance(extensions_data, dict)
        # Should have fetched python extensions
        self.assertIn("python", extensions_data)

    @patch("requests.get")
    def test_complete_data_loading(self, mock_get):
        """Test complete EESSI data loading process."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_software_data
            elif "ext-python.json" in url:
                mock_response.json.return_value = self.eessi_extensions_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06")
        catalog_data = loader.fetch_catalog_data()

        # Verify catalog structure
        self.assertEqual(catalog_data.name, "EESSI")
        self.assertEqual(catalog_data.version, "2023.06")
        self.assertEqual(catalog_data.catalog_type, "binary_runtime")
        self.assertIn("timestamp", catalog_data.metadata)

        # Verify packages were loaded
        self.assertGreater(len(catalog_data.packages), 0)

        # Check that a main software package exists
        self.assertIn("ALL", catalog_data.packages)
        package_data = catalog_data.packages["ALL"]
        self.assertFalse(package_data.package_data.is_extension)

        # Verify versions and targets
        self.assertGreater(len(package_data.versions), 0)
        for version_with_targets in package_data.versions.values():
            self.assertGreater(len(version_with_targets.targets), 0)

            # Verify target structure
            target = version_with_targets.targets[0]
            self.assertEqual(target.target_type, "cpu_architecture")
            self.assertIn(target.target_name, ["x86_64", "aarch64"])

    @patch("requests.get")
    def test_database_loading(self, mock_get):
        """Test loading EESSI data into database models."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_software_data
            elif "ext-python.json" in url:
                mock_response.json.return_value = self.eessi_extensions_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06")

        # Load into database
        stats = loader.load_catalog(update_existing=True, dry_run=False)

        # Verify statistics
        self.assertGreater(stats["packages_created"], 0)
        self.assertGreater(stats["versions_created"], 0)
        self.assertGreater(stats["targets_created"], 0)

        # Verify database objects were created
        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")
        self.assertEqual(catalog.catalog_type, "binary_runtime")
        self.assertIsNotNone(catalog.last_successful_update)

        # Verify packages
        packages = SoftwarePackage.objects.filter(catalog=catalog)
        self.assertGreater(packages.count(), 0)

        # Verify at least one package has versions and targets
        package = packages.first()
        versions = SoftwareVersion.objects.filter(package=package)
        self.assertGreater(versions.count(), 0)

        version = versions.first()
        targets = SoftwareTarget.objects.filter(version=version)
        self.assertGreater(targets.count(), 0)

        # Verify target structure
        target = targets.first()
        self.assertEqual(target.target_type, "cpu_architecture")
        self.assertIn(target.target_name, ["x86_64", "aarch64"])

    def test_dry_run_mode(self):
        """Test dry run mode doesn't create database objects."""
        with patch("requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = self.eessi_software_data
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            loader = EESSICatalogLoader(catalog_version="2023.06")

            # Run in dry run mode
            stats = loader.load_catalog(dry_run=True)

            # Should return statistics but not create objects
            self.assertGreater(stats["packages_created"], 0)
            self.assertEqual(SoftwareCatalog.objects.count(), 0)
            self.assertEqual(SoftwarePackage.objects.count(), 0)

    @patch("requests.get")
    def test_network_error_handling(self, mock_get):
        """Test handling of network errors during data fetching."""
        mock_get.side_effect = requests.exceptions.RequestException("Network error")

        loader = EESSICatalogLoader()

        with self.assertRaises(CatalogLoadError):
            loader.fetch_catalog_data()


class SpackCatalogLoaderTest(BaseLoaderTestCase):
    """Test cases for Spack catalog loader."""

    def setUp(self):
        self.spack_data = self.load_test_fixture("spack_repology_test.json")

    def test_loader_initialization(self):
        """Test Spack loader can be initialized with various parameters."""
        # Test with defaults
        loader = SpackCatalogLoader()
        self.assertEqual(loader.catalog_name, "Spack")
        self.assertEqual(loader.catalog_type, "source_package")

        # Test with custom parameters
        loader = SpackCatalogLoader(
            catalog_name="Custom Spack", catalog_version="2024.11.26"
        )
        self.assertEqual(loader.catalog_name, "Custom Spack")
        self.assertEqual(loader.catalog_version, "2024.11.26")

    @patch("requests.get")
    def test_version_detection_from_timestamp(self, mock_get):
        """Test automatic version detection from Spack timestamp."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "last_update": "2024-11-26 12:00:00.123456",
            "packages": {},
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        loader = SpackCatalogLoader(catalog_version="auto")
        # Should convert timestamp to version format
        self.assertEqual(loader.catalog_version, "2024.11.26")

    @patch("requests.get")
    def test_spack_data_parsing(self, mock_get):
        """Test parsing Spack repology.json format."""
        mock_response = Mock()
        mock_response.json.return_value = self.spack_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        loader = SpackCatalogLoader(catalog_version="test")
        catalog_data = loader.fetch_catalog_data()

        # Verify catalog structure
        self.assertEqual(catalog_data.name, "Spack")
        self.assertEqual(catalog_data.catalog_type, "source_package")
        self.assertIn("last_update", catalog_data.metadata)

        # Verify packages were parsed
        self.assertGreater(len(catalog_data.packages), 0)

        # Check package structure for first package
        first_package_name = list(catalog_data.packages.keys())[0]
        package_data = catalog_data.packages[first_package_name]

        # Verify package metadata
        self.assertIsInstance(package_data.package_data.categories, list)
        self.assertIsInstance(package_data.package_data.licenses, list)
        self.assertIsInstance(package_data.package_data.maintainers, list)
        self.assertFalse(
            package_data.package_data.is_extension
        )  # Spack doesn't use extensions

        # Verify versions
        self.assertGreater(len(package_data.versions), 0)

        # Verify targets (build variants)
        for version_with_targets in package_data.versions.values():
            self.assertGreater(len(version_with_targets.targets), 0)

            # Should have at least default build target
            default_targets = [
                t for t in version_with_targets.targets if t.target_name == "default"
            ]
            self.assertGreater(len(default_targets), 0)

    @patch("requests.get")
    def test_database_loading(self, mock_get):
        """Test loading Spack data into database models."""
        mock_response = Mock()
        mock_response.json.return_value = self.spack_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        loader = SpackCatalogLoader(catalog_version="test")

        # Load into database
        stats = loader.load_catalog(update_existing=True, dry_run=False)

        # Verify statistics
        self.assertGreater(stats["packages_created"], 0)
        self.assertGreater(stats["versions_created"], 0)
        self.assertGreater(stats["targets_created"], 0)

        # Verify database objects
        catalog = SoftwareCatalog.objects.get(name="Spack", version="test")
        self.assertEqual(catalog.catalog_type, "source_package")

        # Verify packages have correct structure
        packages = SoftwarePackage.objects.filter(catalog=catalog)
        self.assertGreater(packages.count(), 0)

        package = packages.first()
        self.assertIsInstance(package.categories, list)
        self.assertIsInstance(package.licenses, list)
        self.assertFalse(package.is_extension)

        # Verify versions have dependencies
        versions = SoftwareVersion.objects.filter(package=package)
        self.assertGreater(versions.count(), 0)

        version = versions.first()
        self.assertIsInstance(version.dependencies, list)
        self.assertIsInstance(version.metadata, dict)

        # Verify targets
        targets = SoftwareTarget.objects.filter(version=version)
        self.assertGreater(targets.count(), 0)

        target = targets.first()
        self.assertIn(
            target.target_type,
            ["build_variant", "platform", "external", "build_system"],
        )


class CatalogLoaderIntegrationTest(BaseLoaderTestCase):
    """Integration tests for catalog loading process."""

    def setUp(self):
        self.eessi_software_data = self.load_test_fixture("eessi_software_test.json")
        self.spack_data = self.load_test_fixture("spack_repology_test.json")

    @patch("requests.get")
    def test_multiple_catalog_loading(self, mock_get):
        """Test loading multiple catalogs without conflicts."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "eessi" in url.lower():
                mock_response.json.return_value = self.eessi_software_data
            elif "spack" in url.lower() or "repology" in url:
                mock_response.json.return_value = self.spack_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        # Load EESSI catalog
        eessi_loader = EESSICatalogLoader(catalog_version="2023.06")
        eessi_loader.load_catalog(dry_run=False)

        # Load Spack catalog
        spack_loader = SpackCatalogLoader(catalog_version="test")
        spack_loader.load_catalog(dry_run=False)

        # Verify both catalogs exist
        self.assertEqual(SoftwareCatalog.objects.count(), 2)

        eessi_catalog = SoftwareCatalog.objects.get(name="EESSI")
        spack_catalog = SoftwareCatalog.objects.get(name="Spack")

        self.assertEqual(eessi_catalog.catalog_type, "binary_runtime")
        self.assertEqual(spack_catalog.catalog_type, "source_package")

        # Verify packages are separate per catalog
        eessi_packages = SoftwarePackage.objects.filter(catalog=eessi_catalog)
        spack_packages = SoftwarePackage.objects.filter(catalog=spack_catalog)

        self.assertGreater(eessi_packages.count(), 0)
        self.assertGreater(spack_packages.count(), 0)

    @patch("requests.get")
    def test_update_existing_catalog(self, mock_get):
        """Test updating an existing catalog preserves structure."""
        mock_response = Mock()
        mock_response.json.return_value = self.eessi_software_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # First load
        loader = EESSICatalogLoader(catalog_version="2023.06")
        loader.load_catalog(dry_run=False)

        original_catalog_count = SoftwareCatalog.objects.count()
        original_package_count = SoftwarePackage.objects.count()

        # Second load (update)
        loader.load_catalog(update_existing=True, dry_run=False)

        # Should not create duplicate catalog
        self.assertEqual(SoftwareCatalog.objects.count(), original_catalog_count)

        # Package count might vary depending on data, but shouldn't dramatically increase
        current_package_count = SoftwarePackage.objects.count()
        self.assertLessEqual(current_package_count, original_package_count * 2)

    def test_error_handling_resilience(self):
        """Test that loader errors are properly handled and cataloged."""
        # Test with invalid API URL
        loader = EESSICatalogLoader(api_base_url="invalid://url")

        with self.assertRaises(CatalogLoadError):
            loader.fetch_catalog_data()

    @freeze_time("2024-11-26 12:00:00")
    def test_catalog_update_timestamps(self):
        """Test that catalog update timestamps are properly recorded."""
        with patch("requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = self.eessi_software_data
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            loader = EESSICatalogLoader(catalog_version="2023.06")
            loader.load_catalog(dry_run=False)

            catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")

            # Verify timestamps
            self.assertIsNotNone(catalog.last_successful_update)
            self.assertEqual(catalog.last_successful_update, timezone.now())
            self.assertEqual(catalog.update_errors, "")


class EESSINewAPIFormatTest(BaseLoaderTestCase):
    """Test cases for EESSI new API format with dict-based module and required_modules."""

    def setUp(self):
        self.eessi_new_format_data = self.load_test_fixture(
            "eessi_new_format_test.json"
        )

    @patch("requests.get")
    def test_new_format_module_dict_parsing(self, mock_get):
        """Test that new format module dict is parsed correctly."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_new_format_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        # Check that NewFormatPackage is loaded
        self.assertIn("NewFormatPackage", catalog_data.packages)
        package = catalog_data.packages["NewFormatPackage"]
        version = package.versions["3.0.0-foss-2023b"]

        # Verify module is a dict with correct structure
        module = version.version_data.metadata.get("module", {})
        self.assertIsInstance(module, dict)
        self.assertEqual(
            module.get("full_module_name"), "NewFormatPackage/3.0.0-foss-2023b"
        )
        self.assertEqual(module.get("module_name"), "NewFormatPackage")
        self.assertEqual(module.get("module_version"), "3.0.0-foss-2023b")

    @patch("requests.get")
    def test_new_format_required_modules_dict_list(self, mock_get):
        """Test that new format required_modules (list of dicts) is parsed correctly."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_new_format_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        package = catalog_data.packages["NewFormatPackage"]
        version = package.versions["3.0.0-foss-2023b"]

        # Verify required_modules is a list of dicts
        required_modules = version.version_data.metadata.get("required_modules", [])
        self.assertIsInstance(required_modules, list)
        self.assertGreater(len(required_modules), 0)

        # Check structure of first required module
        first_rm = required_modules[0]
        self.assertIsInstance(first_rm, dict)
        self.assertEqual(first_rm.get("full_module_name"), "EESSI/2023.06")
        self.assertEqual(first_rm.get("module_name"), "EESSI")
        self.assertEqual(first_rm.get("module_version"), "2023.06")

    @patch("requests.get")
    def test_new_format_extensions_field(self, mock_get):
        """Test that new format extensions field is parsed correctly."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_new_format_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        # Check package with extensions
        package = catalog_data.packages["PackageWithExtensions"]
        version = package.versions["2.0.0-gfbf-2023b"]

        extensions = version.version_data.metadata.get("extensions", [])
        self.assertIsInstance(extensions, list)
        self.assertEqual(len(extensions), 2)

        # Verify extension structure
        ext_types = {ext.get("type") for ext in extensions}
        self.assertEqual(ext_types, {"python", "component"})

        python_ext = next(ext for ext in extensions if ext.get("type") == "python")
        self.assertEqual(python_ext.get("name"), "gmxapi")
        self.assertEqual(python_ext.get("version"), "0.4.2")

        # Check package without extensions
        package_no_ext = catalog_data.packages["PackageNoExtensions"]
        version_no_ext = package_no_ext.versions["1.5.0-foss-2023a"]
        extensions_no_ext = version_no_ext.version_data.metadata.get("extensions", [])
        self.assertEqual(extensions_no_ext, [])

    @patch("requests.get")
    def test_new_format_database_loading(self, mock_get):
        """Test loading new format EESSI data into database models."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_new_format_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        stats = loader.load_catalog(update_existing=True, dry_run=False)

        # Verify statistics
        self.assertGreater(stats["packages_created"], 0)
        self.assertGreater(stats["versions_created"], 0)

        # Verify database objects
        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")

        # Check package with extensions was loaded
        package = SoftwarePackage.objects.get(
            catalog=catalog, name="PackageWithExtensions"
        )
        version = SoftwareVersion.objects.get(
            package=package,
            version="2.0.0",
            module_version="2.0.0-gfbf-2023b",
        )

        # Verify metadata contains new fields
        self.assertIn("module", version.metadata)
        self.assertIn("extensions", version.metadata)
        self.assertIn("required_modules", version.metadata)

        # Verify extensions field
        extensions = version.metadata["extensions"]
        self.assertEqual(len(extensions), 2)

        # Verify module structure
        module = version.metadata["module"]
        self.assertEqual(module["module_name"], "PackageWithExtensions")


class EESSIExtensionMultipleParentsTest(BaseLoaderTestCase):
    """Test that extensions with multiple parent software packages are loaded correctly.

    Reproduces the bug where adwaita-icon-theme should have both GTK3 and GTK4
    as parents but only gets one (or zero) due to prefixed dict keys in
    _process_extension_batch not matching actual DB package names.
    """

    def setUp(self):
        self.eessi_software_data = self.load_test_fixture("eessi_software_test.json")
        self.eessi_component_data = self.load_test_fixture(
            "eessi_extensions_component_test.json"
        )

    def _make_mock_get(self):
        """Create mock that returns software + component extension data."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_software_data
            elif "ext-component.json" in url:
                mock_response.json.return_value = self.eessi_component_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        return mock_requests_side_effect

    @patch("requests.get")
    def test_extension_with_two_parents_loaded_into_database(self, mock_get):
        """adwaita-icon-theme should have both GTK3 and GTK4 as parent_softwares."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2023.06")
        loader.load_catalog(update_existing=True, dry_run=False)

        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")

        # Both parent packages must exist as main packages
        gtk3 = SoftwarePackage.objects.get(catalog=catalog, name="GTK3")
        gtk4 = SoftwarePackage.objects.get(catalog=catalog, name="GTK4")
        self.assertFalse(gtk3.is_extension)
        self.assertFalse(gtk4.is_extension)

        # adwaita-icon-theme must exist as an extension
        adwaita = SoftwarePackage.objects.get(
            catalog=catalog, name="adwaita-icon-theme"
        )
        self.assertTrue(adwaita.is_extension)

        # Must have BOTH parents
        parent_names = set(adwaita.parent_softwares.values_list("name", flat=True))
        self.assertEqual(parent_names, {"GTK3", "GTK4"})

    @patch("requests.get")
    def test_extension_versions_are_created(self, mock_get):
        """Extension packages must have their versions created in the DB."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2023.06")
        loader.load_catalog(update_existing=True, dry_run=False)

        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")
        adwaita = SoftwarePackage.objects.get(
            catalog=catalog, name="adwaita-icon-theme"
        )

        # Must have versions from the fixture (42.0 from GTK3, 45.0 from GTK4)
        version_names = set(
            SoftwareVersion.objects.filter(package=adwaita).values_list(
                "version", flat=True
            )
        )
        self.assertIn("42.0", version_names)
        self.assertIn("45.0", version_names)

    @patch("requests.get")
    def test_single_parent_extension_works(self, mock_get):
        """hicolor-icon-theme has only GTK3 as parent — should still work."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2023.06")
        loader.load_catalog(update_existing=True, dry_run=False)

        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")
        hicolor = SoftwarePackage.objects.get(
            catalog=catalog, name="hicolor-icon-theme"
        )
        self.assertTrue(hicolor.is_extension)

        parent_names = set(hicolor.parent_softwares.values_list("name", flat=True))
        self.assertEqual(parent_names, {"GTK3"})

    @patch("requests.get")
    def test_reload_preserves_multiple_parents(self, mock_get):
        """Loading twice should preserve (not lose) parent relationships."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2023.06")
        loader.load_catalog(update_existing=True, dry_run=False)

        # Second load
        loader2 = EESSICatalogLoader(catalog_version="2023.06")
        loader2.load_catalog(update_existing=True, dry_run=False)

        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")
        adwaita = SoftwarePackage.objects.get(
            catalog=catalog, name="adwaita-icon-theme"
        )
        parent_names = set(adwaita.parent_softwares.values_list("name", flat=True))
        self.assertEqual(parent_names, {"GTK3", "GTK4"})


class CatalogLoaderErrorHandlingTest(TestCase):
    """Test error handling and resilience of catalog loaders."""

    def test_network_timeout_handling(self):
        """Test handling of network timeouts."""
        with patch("requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Request timed out")

            loader = EESSICatalogLoader()

            with self.assertRaises(CatalogLoadError):
                loader.fetch_catalog_data()

    def test_invalid_json_handling(self):
        """Test handling of invalid JSON responses."""
        with patch("requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            loader = SpackCatalogLoader()

            with self.assertRaises(CatalogLoadError):
                loader.fetch_catalog_data()

    def test_http_error_handling(self):
        """Test handling of HTTP errors (404, 500, etc.)."""
        with patch("requests.get") as mock_get:
            mock_response = Mock()
            mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
                "404 Not Found"
            )
            mock_get.return_value = mock_response

            loader = EESSICatalogLoader()

            with self.assertRaises(CatalogLoadError):
                loader.fetch_catalog_data()

    def test_database_error_recovery(self):
        """Test that database errors are properly handled."""
        with patch("requests.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = {"software": {"test": {"versions": []}}}
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            loader = EESSICatalogLoader(catalog_version="2023.06")

            # Simulate database error during loading (management command path
            # uses filter().first() + create(); patch create to fail)
            with patch.object(
                SoftwareCatalog.objects,
                "create",
                side_effect=Exception("DB Error"),
            ):
                with self.assertRaises(CatalogLoadError):
                    loader.load_catalog(dry_run=False)


class EESSIVersionFilteringTest(BaseLoaderTestCase):
    """Test that EESSI loader correctly filters versions by EESSI version."""

    def setUp(self):
        self.eessi_software_data = self.load_test_fixture("eessi_software_test.json")

    def _make_mock_get(self):
        """Create a mock requests.get that returns the test fixture data."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.eessi_software_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        return mock_requests_side_effect

    @patch("requests.get")
    def test_loader_2023_06_only_includes_2023_06_versions(self, mock_get):
        """Test that loader with catalog_version=2023.06 only includes EESSI 2023.06 versions."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        # ALL, AOFlagger, ASE are 2023.06 only — should be present
        self.assertIn("ALL", catalog_data.packages)
        self.assertIn("AOFlagger", catalog_data.packages)
        self.assertIn("ASE", catalog_data.packages)

        # JupyterLab has versions in both — only 4.0.5 is 2023.06
        self.assertIn("JupyterLab", catalog_data.packages)
        jupyterlab = catalog_data.packages["JupyterLab"]
        self.assertIn("4.0.5", jupyterlab.versions)
        self.assertNotIn("4.2.5", jupyterlab.versions)

        # NewTool2025 is 2025.06 only — should NOT be present
        self.assertNotIn("NewTool2025", catalog_data.packages)

    @patch("requests.get")
    def test_loader_2025_06_only_includes_2025_06_versions(self, mock_get):
        """Test that loader with catalog_version=2025.06 only includes EESSI 2025.06 versions."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2025.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        # ALL, AOFlagger, ASE are 2023.06 only — should NOT be present
        self.assertNotIn("ALL", catalog_data.packages)
        self.assertNotIn("AOFlagger", catalog_data.packages)
        self.assertNotIn("ASE", catalog_data.packages)

        # JupyterLab has versions in both — only 4.2.5 is 2025.06
        self.assertIn("JupyterLab", catalog_data.packages)
        jupyterlab = catalog_data.packages["JupyterLab"]
        self.assertNotIn("4.0.5", jupyterlab.versions)
        self.assertIn("4.2.5", jupyterlab.versions)

        # NewTool2025 is 2025.06 only — should be present
        self.assertIn("NewTool2025", catalog_data.packages)

    @patch("requests.get")
    def test_packages_with_zero_matching_versions_excluded(self, mock_get):
        """Test that packages with no matching versions are excluded entirely."""
        mock_get.side_effect = self._make_mock_get()

        loader = EESSICatalogLoader(catalog_version="2025.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        # ALL only has a 2023.06 version, so should be excluded from 2025.06 catalog
        self.assertNotIn("ALL", catalog_data.packages)

    @patch("requests.get")
    def test_new_format_version_filtering(self, mock_get):
        """Test version filtering with new dict-based required_modules format."""
        new_format_data = self.load_test_fixture("eessi_new_format_test.json")

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = new_format_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        # All packages in new_format_test.json are 2023.06
        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()
        self.assertIn("NewFormatPackage", catalog_data.packages)
        self.assertIn("PackageWithExtensions", catalog_data.packages)

        # None should appear under 2025.06
        loader_2025 = EESSICatalogLoader(
            catalog_version="2025.06", include_extensions=False
        )
        catalog_data_2025 = loader_2025.fetch_catalog_data()
        self.assertEqual(len(catalog_data_2025.packages), 0)

    @patch("requests.get")
    def test_sync_removes_stale_versions_from_database(self, mock_get):
        """Test that sync=True removes stale versions wrongly in the DB."""
        mock_get.side_effect = self._make_mock_get()

        # First load with no filtering (simulate the old buggy behavior)
        # by loading all data as 2023.06
        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        stats = loader.load_catalog(update_existing=True, dry_run=False)
        self.assertGreater(stats["packages_created"], 0)

        catalog = SoftwareCatalog.objects.get(name="EESSI")

        # JupyterLab should have only version 4.0.5 (2023.06)
        jupyterlab_pkg = SoftwarePackage.objects.get(catalog=catalog, name="JupyterLab")
        self.assertEqual(
            SoftwareVersion.objects.filter(package=jupyterlab_pkg).count(), 1
        )
        self.assertTrue(
            SoftwareVersion.objects.filter(
                package=jupyterlab_pkg, version="4.0.5"
            ).exists()
        )

        # Manually create a stale version (simulating old buggy load)
        SoftwareVersion.objects.create(
            package=jupyterlab_pkg,
            version="4.2.5",
            dependencies=[],
            metadata={},
        )
        self.assertEqual(
            SoftwareVersion.objects.filter(package=jupyterlab_pkg).count(), 2
        )

        # Re-load with sync=True to clean up
        loader2 = EESSICatalogLoader(
            catalog_version="2023.06", include_extensions=False
        )
        stats2 = loader2.load_catalog(
            update_existing=True, dry_run=False, catalog=catalog, sync=True
        )

        # Stale version should be deleted
        self.assertGreater(stats2["versions_deleted"], 0)
        self.assertEqual(
            SoftwareVersion.objects.filter(package=jupyterlab_pkg).count(), 1
        )
        self.assertTrue(
            SoftwareVersion.objects.filter(
                package=jupyterlab_pkg, version="4.0.5"
            ).exists()
        )
        self.assertFalse(
            SoftwareVersion.objects.filter(
                package=jupyterlab_pkg, version="4.2.5"
            ).exists()
        )

    @patch("requests.get")
    def test_sync_removes_stale_packages_from_database(self, mock_get):
        """Test that sync=True removes packages not in incoming data."""
        mock_get.side_effect = self._make_mock_get()

        # Load with 2023.06 version
        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        loader.load_catalog(update_existing=True, dry_run=False)

        catalog = SoftwareCatalog.objects.get(name="EESSI")

        # Manually create a stale package (simulating old buggy load)
        SoftwarePackage.objects.create(
            catalog=catalog,
            name="StalePackage",
            description="Should not be here",
        )

        # Re-load with sync=True to clean up
        loader2 = EESSICatalogLoader(
            catalog_version="2023.06", include_extensions=False
        )
        stats2 = loader2.load_catalog(
            update_existing=True, dry_run=False, catalog=catalog, sync=True
        )

        self.assertGreater(stats2["packages_deleted"], 0)
        self.assertFalse(
            SoftwarePackage.objects.filter(
                catalog=catalog, name="StalePackage"
            ).exists()
        )


class EESSIGromacsDuplicateVersionsTest(BaseLoaderTestCase):
    """Test that EESSI builds with the same upstream version are all loaded.

    Example: GROMACS 2024.4 is published twice in EESSI - with and without CUDA.
    Both share version='2024.4' but have different module_version values.
    """

    def setUp(self):
        self.gromacs_data = self.load_test_fixture(
            "eessi_gromacs_duplicate_versions_test.json"
        )

    @patch("requests.get")
    def test_gromacs_duplicate_upstream_versions_parsed(self, mock_get):
        """Both GROMACS 2024.4 builds are kept when parsing catalog data."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.gromacs_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        catalog_data = loader.fetch_catalog_data()

        package = catalog_data.packages["GROMACS"]
        self.assertEqual(len(package.versions), 2)
        self.assertIn("2024.4-foss-2023b", package.versions)
        self.assertIn("2024.4-foss-2023b-CUDA-12.4.0", package.versions)

        cpu_build = package.versions["2024.4-foss-2023b"]
        cuda_build = package.versions["2024.4-foss-2023b-CUDA-12.4.0"]
        self.assertEqual(cpu_build.version_data.version, "2024.4")
        self.assertEqual(cuda_build.version_data.version, "2024.4")
        self.assertEqual(cpu_build.version_data.module_version, "2024.4-foss-2023b")
        self.assertEqual(
            cuda_build.version_data.module_version,
            "2024.4-foss-2023b-CUDA-12.4.0",
        )

    @patch("requests.get")
    def test_gromacs_duplicate_upstream_versions_loaded_to_database(self, mock_get):
        """Both GROMACS 2024.4 builds are persisted as separate SoftwareVersion rows."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "software.json" in url:
                mock_response.json.return_value = self.gromacs_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        loader = EESSICatalogLoader(catalog_version="2023.06", include_extensions=False)
        stats = loader.load_catalog(update_existing=True, dry_run=False)

        self.assertEqual(stats["packages_created"], 1)
        self.assertEqual(stats["versions_created"], 2)

        catalog = SoftwareCatalog.objects.get(name="EESSI", version="2023.06")
        package = SoftwarePackage.objects.get(catalog=catalog, name="GROMACS")
        versions = SoftwareVersion.objects.filter(package=package).order_by(
            "module_version"
        )
        self.assertEqual(versions.count(), 2)

        module_versions = list(versions.values_list("version", "module_version"))
        self.assertEqual(
            module_versions,
            [
                ("2024.4", "2024.4-foss-2023b"),
                ("2024.4", "2024.4-foss-2023b-CUDA-12.4.0"),
            ],
        )

        cuda_version = versions.get(module_version="2024.4-foss-2023b-CUDA-12.4.0")
        self.assertTrue(cuda_version.targets.exclude(gpu_architectures=[]).exists())
        cpu_version = versions.get(module_version="2024.4-foss-2023b")
        self.assertFalse(cpu_version.targets.exclude(gpu_architectures=[]).exists())
