"""
Test cases for software catalog Celery tasks.

Tests the master catalog update task and its exception handling behavior
to ensure individual catalog failures don't prevent other catalogs from updating.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from constance.test.unittest import override_config
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_mastermind.marketplace.models import SoftwareCatalog
from waldur_mastermind.marketplace.tasks import (
    _update_catalog_with_error_handling,
    _validate_catalog_config,
    update_software_catalogs,
)


class CatalogTasksTest(TestCase):
    """Test cases for catalog update tasks."""

    def setUp(self):
        self.fixtures_dir = Path(__file__).parent / "fixtures" / "catalog_data"

        # Load test data
        with open(self.fixtures_dir / "eessi_software_test.json") as f:
            self.eessi_data = json.load(f)

        with open(self.fixtures_dir / "spack_repology_test.json") as f:
            self.spack_data = json.load(f)

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_EESSI_API_URL="https://test.eessi.io/",
        SOFTWARE_CATALOG_SPACK_DATA_URL="https://test.spack.io/data.json",
        SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES=True,
    )
    @patch("requests.get")
    def test_successful_catalog_updates(self, mock_get):
        """Test successful update of all catalogs."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "eessi" in url.lower():
                mock_response.json.return_value = self.eessi_data
            elif "spack" in url.lower():
                mock_response.json.return_value = self.spack_data
            else:
                mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        # Run the master task
        result = update_software_catalogs()

        # Verify successful completion
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["catalogs_updated"], 2)
        self.assertEqual(result["catalogs_failed"], 0)
        self.assertEqual(result["catalogs_skipped"], 0)

        # Verify individual results
        self.assertEqual(result["results"]["eessi"]["status"], "success")
        self.assertEqual(result["results"]["spack"]["status"], "success")

        # Verify database objects were created
        self.assertEqual(SoftwareCatalog.objects.count(), 2)

        eessi_catalog = SoftwareCatalog.objects.get(name="EESSI")
        spack_catalog = SoftwareCatalog.objects.get(name="Spack")

        self.assertEqual(eessi_catalog.catalog_type, "binary_runtime")
        self.assertEqual(spack_catalog.catalog_type, "source_package")

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_EESSI_API_URL="https://test.eessi.io/",
        SOFTWARE_CATALOG_SPACK_DATA_URL="https://test.spack.io/data.json",
    )
    @patch("requests.get")
    def test_partial_failure_resilience(self, mock_get):
        """Test that one catalog failure doesn't prevent others from updating."""

        def mock_requests_side_effect(url, **kwargs):
            mock_response = Mock()
            if "eessi" in url.lower():
                # EESSI fails
                mock_response.raise_for_status.side_effect = (
                    requests.exceptions.HTTPError("500 Server Error")
                )
            elif "spack" in url.lower():
                # Spack succeeds
                mock_response.json.return_value = self.spack_data
                mock_response.raise_for_status.return_value = None
            return mock_response

        mock_get.side_effect = mock_requests_side_effect

        # Run the master task
        result = update_software_catalogs()

        # Verify partial completion
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["catalogs_updated"], 1)  # Only Spack succeeded
        self.assertEqual(result["catalogs_failed"], 1)  # EESSI failed
        self.assertEqual(result["catalogs_skipped"], 0)

        # Verify individual results
        self.assertEqual(result["results"]["eessi"]["status"], "error")
        self.assertEqual(result["results"]["spack"]["status"], "success")

        # Verify that successful catalog still created database objects
        self.assertEqual(SoftwareCatalog.objects.count(), 1)
        spack_catalog = SoftwareCatalog.objects.get(name="Spack")
        self.assertEqual(spack_catalog.catalog_type, "source_package")

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=False,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_SPACK_DATA_URL="https://test.spack.io/data.json",
    )
    @patch("requests.get")
    def test_selective_catalog_updates(self, mock_get):
        """Test updating only enabled catalogs."""
        mock_response = Mock()
        mock_response.json.return_value = self.spack_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Run the master task
        result = update_software_catalogs()

        # Verify selective completion
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["catalogs_updated"], 1)  # Only Spack
        self.assertEqual(result["catalogs_failed"], 0)
        self.assertEqual(result["catalogs_skipped"], 1)  # EESSI skipped

        # Verify results
        self.assertEqual(result["results"]["eessi"]["status"], "skipped")
        self.assertEqual(result["results"]["spack"]["status"], "success")

        # Verify only enabled catalog was processed
        self.assertEqual(SoftwareCatalog.objects.count(), 1)
        spack_catalog = SoftwareCatalog.objects.get(name="Spack")
        self.assertEqual(spack_catalog.catalog_type, "source_package")

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_EESSI_API_URL="",  # Invalid URL
        SOFTWARE_CATALOG_SPACK_DATA_URL="https://test.spack.io/data.json",
    )
    @patch("requests.get")
    def test_configuration_validation_errors(self, mock_get):
        """Test that configuration validation prevents runtime errors."""
        mock_response = Mock()
        mock_response.json.return_value = self.spack_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Run the master task
        result = update_software_catalogs()

        # Should fail EESSI due to config validation, succeed Spack
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["catalogs_updated"], 1)  # Spack
        self.assertEqual(result["catalogs_failed"], 1)  # EESSI config error

        # Verify error details
        self.assertEqual(result["results"]["eessi"]["status"], "error")
        self.assertIn(
            "Configuration validation failed", result["results"]["eessi"]["error"]
        )
        self.assertEqual(result["results"]["spack"]["status"], "success")

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
    )
    @patch("requests.get")
    def test_all_catalogs_fail_scenario(self, mock_get):
        """Test behavior when all catalogs fail to update."""
        # All requests fail
        mock_get.side_effect = requests.exceptions.ConnectionError(
            "Network unreachable"
        )

        # Run the master task
        result = update_software_catalogs()

        # Verify failed status
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["catalogs_updated"], 0)
        self.assertEqual(result["catalogs_failed"], 2)
        self.assertEqual(result["catalogs_skipped"], 0)

        # Verify all catalogs show error status
        self.assertEqual(result["results"]["eessi"]["status"], "error")
        self.assertEqual(result["results"]["spack"]["status"], "error")

        # Verify no database objects were created
        self.assertEqual(SoftwareCatalog.objects.count(), 0)

    def test_catalog_config_validation(self):
        """Test configuration validation function."""
        # Valid EESSI config
        valid_eessi_config = {
            "name": "EESSI",
            "loader_kwargs": {
                "catalog_name": "EESSI",
                "api_base_url": "https://www.eessi.io/api_data/data/",
            },
        }
        errors = _validate_catalog_config(valid_eessi_config)
        self.assertEqual(errors, [])

        # Invalid EESSI config (bad URL)
        invalid_eessi_config = {
            "name": "EESSI",
            "loader_kwargs": {
                "catalog_name": "EESSI",
                "api_base_url": "invalid-url",
            },
        }
        errors = _validate_catalog_config(invalid_eessi_config)
        self.assertGreater(len(errors), 0)
        self.assertIn("valid HTTP/HTTPS URL", errors[0])

        # Valid Spack config
        valid_spack_config = {
            "name": "Spack",
            "loader_kwargs": {
                "catalog_name": "Spack",
                "data_url": "https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json",
            },
        }
        errors = _validate_catalog_config(valid_spack_config)
        self.assertEqual(errors, [])

        # Missing catalog name
        missing_name_config = {"name": "Test", "loader_kwargs": {}}
        errors = _validate_catalog_config(missing_name_config)
        self.assertGreater(len(errors), 0)
        self.assertIn("Catalog name is required", errors[0])

    @patch("waldur_mastermind.marketplace.tasks.EESSICatalogLoader")
    def test_loader_instantiation_error_handling(self, mock_loader_class):
        """Test handling of loader instantiation errors."""
        # Make loader instantiation fail
        mock_loader_class.side_effect = Exception("Loader initialization failed")

        with override_config(
            SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
            SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=False,
        ):
            result = update_software_catalogs()

        # Should handle the error gracefully
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["catalogs_failed"], 1)
        self.assertEqual(result["results"]["eessi"]["status"], "error")
        self.assertIn(
            "Failed to initialize EESSI loader", result["results"]["eessi"]["error"]
        )

    @freeze_time("2024-11-26 12:00:00")
    def test_catalog_error_tracking(self):
        """Test that catalog errors are properly tracked in database."""
        # Create a catalog that will fail
        catalog = SoftwareCatalog.objects.create(
            name="Test", version="1.0", catalog_type="binary_runtime"
        )

        # Mock loader that fails
        mock_loader = Mock()
        mock_loader.catalog_version = "1.0"
        mock_loader.load_catalog.side_effect = Exception("Test failure")

        # Call the error handling function
        with self.assertRaises(Exception):
            _update_catalog_with_error_handling(
                loader=mock_loader, catalog_name="Test", catalog_type="binary_runtime"
            )

        # Verify error was recorded in catalog
        catalog.refresh_from_db()
        self.assertIsNotNone(catalog.last_update_attempt)
        self.assertIn("Test failure", catalog.update_errors)
        self.assertIsNone(catalog.last_successful_update)

    def test_catalog_update_reuses_existing_catalog(self):
        """Test that catalog updates reuse existing catalog instead of creating new one."""
        # Create an existing catalog
        existing_catalog = SoftwareCatalog.objects.create(
            name="Spack",
            version="2025.01.01",  # Old version
            catalog_type="source_package",
        )
        original_pk = existing_catalog.pk

        # Mock loader with new version
        mock_loader = Mock()
        mock_loader.catalog_version = "2026.01.25"  # New version
        mock_loader.load_catalog.return_value = {
            "packages_created": 10,
            "versions_created": 50,
            "targets_created": 100,
        }

        # Call the update function
        with override_config(SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES=True):
            result_catalog = _update_catalog_with_error_handling(
                loader=mock_loader, catalog_name="Spack", catalog_type="source_package"
            )

        # Verify the same catalog record was updated
        self.assertEqual(result_catalog.pk, original_pk)
        self.assertEqual(result_catalog.version, "2026.01.25")

        # Verify no new catalog was created
        self.assertEqual(
            SoftwareCatalog.objects.filter(
                name="Spack", catalog_type="source_package"
            ).count(),
            1,
        )


class CatalogTaskPerformanceTest(TestCase):
    """Performance and resource usage tests for catalog tasks."""

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=False,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=False,
    )
    def test_disabled_catalogs_performance(self):
        """Test that disabled catalogs are processed quickly."""
        start_time = timezone.now()

        result = update_software_catalogs()

        end_time = timezone.now()
        duration = (end_time - start_time).total_seconds()

        # Should complete very quickly when all catalogs disabled
        self.assertLess(duration, 1.0)  # Less than 1 second

        # Verify results
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["catalogs_skipped"], 2)
        self.assertEqual(result["catalogs_updated"], 0)
        self.assertEqual(result["catalogs_failed"], 0)

    @patch("waldur_mastermind.marketplace.tasks.EESSICatalogLoader")
    @patch("waldur_mastermind.marketplace.tasks.SpackCatalogLoader")
    def test_loader_isolation(self, mock_spack_loader, mock_eessi_loader):
        """Test that loaders are properly isolated from each other."""
        # Setup mock loaders
        mock_eessi_instance = Mock()
        mock_eessi_instance.catalog_version = "2023.06"
        mock_eessi_instance.load_catalog.return_value = {"packages_created": 10}
        mock_eessi_loader.return_value = mock_eessi_instance

        mock_spack_instance = Mock()
        mock_spack_instance.catalog_version = "latest"
        mock_spack_instance.load_catalog.return_value = {"packages_created": 20}
        mock_spack_loader.return_value = mock_spack_instance

        with override_config(
            SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
            SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        ):
            result = update_software_catalogs()

        # Verify both loaders were called independently
        mock_eessi_loader.assert_called_once()
        mock_spack_loader.assert_called_once()

        # Verify successful completion
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["catalogs_updated"], 2)

    @patch("waldur_mastermind.marketplace.tasks.EESSICatalogLoader")
    @patch("waldur_mastermind.marketplace.tasks.SpackCatalogLoader")
    def test_first_catalog_failure_continues_processing(
        self, mock_spack_loader, mock_eessi_loader
    ):
        """Test that first catalog failure doesn't prevent second catalog processing."""
        # Setup EESSI to fail
        mock_eessi_loader.side_effect = Exception("EESSI network error")

        # Setup Spack to succeed
        mock_spack_instance = Mock()
        mock_spack_instance.catalog_version = "latest"
        mock_spack_instance.load_catalog.return_value = {"packages_created": 20}
        mock_spack_loader.return_value = mock_spack_instance

        with override_config(
            SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
            SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        ):
            result = update_software_catalogs()

        # Verify partial completion (EESSI failed, Spack succeeded)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["catalogs_updated"], 1)  # Spack
        self.assertEqual(result["catalogs_failed"], 1)  # EESSI

        # Verify specific results
        self.assertEqual(result["results"]["eessi"]["status"], "error")
        self.assertEqual(result["results"]["spack"]["status"], "success")

        # Verify Spack was still processed despite EESSI failure
        mock_spack_loader.assert_called_once()
        mock_spack_instance.load_catalog.assert_called_once()

    @patch("waldur_mastermind.marketplace.tasks.EESSICatalogLoader")
    @patch("waldur_mastermind.marketplace.tasks.SpackCatalogLoader")
    def test_second_catalog_failure_after_first_success(
        self, mock_spack_loader, mock_eessi_loader
    ):
        """Test that second catalog failure is handled after first success."""
        # Setup EESSI to succeed
        mock_eessi_instance = Mock()
        mock_eessi_instance.catalog_version = "2023.06"
        mock_eessi_instance.load_catalog.return_value = {"packages_created": 10}
        mock_eessi_loader.return_value = mock_eessi_instance

        # Setup Spack to fail
        mock_spack_loader.side_effect = Exception("Spack parsing error")

        with override_config(
            SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
            SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        ):
            result = update_software_catalogs()

        # Verify partial completion (EESSI succeeded, Spack failed)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["catalogs_updated"], 1)  # EESSI
        self.assertEqual(result["catalogs_failed"], 1)  # Spack

        # Verify specific results
        self.assertEqual(result["results"]["eessi"]["status"], "success")
        self.assertEqual(result["results"]["spack"]["status"], "error")

        # Verify EESSI was processed successfully despite Spack failure
        mock_eessi_loader.assert_called_once()
        mock_eessi_instance.load_catalog.assert_called_once()

    @override_config(
        SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED=True,
        SOFTWARE_CATALOG_EESSI_API_URL="",  # Invalid
        SOFTWARE_CATALOG_SPACK_DATA_URL="",  # Invalid
    )
    def test_all_catalogs_fail_due_to_configuration(self):
        """Test behavior when all catalogs fail due to configuration errors."""
        result = update_software_catalogs()

        # Verify failed status
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["catalogs_updated"], 0)
        self.assertEqual(result["catalogs_failed"], 2)
        self.assertEqual(result["catalogs_skipped"], 0)

        # Verify all show configuration errors
        for catalog_result in result["results"].values():
            self.assertEqual(catalog_result["status"], "error")
            self.assertIn("Configuration validation failed", catalog_result["error"])


class CatalogCleanupTasksTest(TestCase):
    """Test cases for software catalog cleanup task."""

    def setUp(self):
        """Create test catalogs with different ages."""
        from datetime import timedelta

        now = timezone.now()

        # Create a recent catalog (should be kept)
        self.recent_catalog = SoftwareCatalog.objects.create(
            name="Spack",
            version="2026.01.25",
            catalog_type="source_package",
            last_successful_update=now - timedelta(days=1),
        )

        # Create an old catalog (should be deleted with default 90 days retention)
        self.old_catalog = SoftwareCatalog.objects.create(
            name="OldCatalog",
            version="2025.10.01",
            catalog_type="source_package",
            last_successful_update=now - timedelta(days=100),
        )

    @override_config(
        SOFTWARE_CATALOG_CLEANUP_ENABLED=True, SOFTWARE_CATALOG_RETENTION_DAYS=90
    )
    def test_cleanup_deletes_old_catalogs(self):
        """Test that cleanup deletes catalogs older than retention period."""
        from waldur_mastermind.marketplace.tasks import cleanup_old_software_catalogs

        result = cleanup_old_software_catalogs()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(len(result["deleted_catalogs"]), 1)
        self.assertEqual(result["deleted_catalogs"][0]["name"], "OldCatalog")

        # Recent catalog should still exist
        self.assertTrue(
            SoftwareCatalog.objects.filter(pk=self.recent_catalog.pk).exists()
        )
        # Old catalog should be deleted
        self.assertFalse(
            SoftwareCatalog.objects.filter(pk=self.old_catalog.pk).exists()
        )

    @override_config(SOFTWARE_CATALOG_CLEANUP_ENABLED=False)
    def test_cleanup_disabled_does_nothing(self):
        """Test that cleanup does nothing when disabled."""
        from waldur_mastermind.marketplace.tasks import cleanup_old_software_catalogs

        result = cleanup_old_software_catalogs()

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["deleted_count"], 0)

        # Both catalogs should still exist
        self.assertTrue(
            SoftwareCatalog.objects.filter(pk=self.recent_catalog.pk).exists()
        )
        self.assertTrue(SoftwareCatalog.objects.filter(pk=self.old_catalog.pk).exists())

    @override_config(
        SOFTWARE_CATALOG_CLEANUP_ENABLED=True, SOFTWARE_CATALOG_RETENTION_DAYS=30
    )
    def test_cleanup_respects_retention_days_setting(self):
        """Test that cleanup respects the retention days setting."""
        from datetime import timedelta

        from waldur_mastermind.marketplace.tasks import cleanup_old_software_catalogs

        # Create a catalog that's 50 days old (should be deleted with 30 day retention)
        medium_old_catalog = SoftwareCatalog.objects.create(
            name="MediumOld",
            version="2025.12.01",
            catalog_type="source_package",
            last_successful_update=timezone.now() - timedelta(days=50),
        )

        result = cleanup_old_software_catalogs()

        self.assertEqual(result["status"], "success")
        # Both old catalogs should be deleted (100 days and 50 days old, both > 30 days)
        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(result["retention_days"], 30)

        # Recent catalog should still exist
        self.assertTrue(
            SoftwareCatalog.objects.filter(pk=self.recent_catalog.pk).exists()
        )
        # Old catalogs should be deleted
        self.assertFalse(
            SoftwareCatalog.objects.filter(pk=self.old_catalog.pk).exists()
        )
        self.assertFalse(
            SoftwareCatalog.objects.filter(pk=medium_old_catalog.pk).exists()
        )

    @override_config(
        SOFTWARE_CATALOG_CLEANUP_ENABLED=True, SOFTWARE_CATALOG_RETENTION_DAYS=200
    )
    def test_cleanup_with_large_retention_keeps_all(self):
        """Test that cleanup keeps all catalogs when retention is very large."""
        from waldur_mastermind.marketplace.tasks import cleanup_old_software_catalogs

        result = cleanup_old_software_catalogs()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_count"], 0)

        # All catalogs should still exist
        self.assertTrue(
            SoftwareCatalog.objects.filter(pk=self.recent_catalog.pk).exists()
        )
        self.assertTrue(SoftwareCatalog.objects.filter(pk=self.old_catalog.pk).exists())

    @override_config(
        SOFTWARE_CATALOG_CLEANUP_ENABLED=True, SOFTWARE_CATALOG_RETENTION_DAYS=200
    )
    def test_cleanup_removes_duplicate_catalogs(self):
        """Test that cleanup removes duplicate catalogs, keeping only the newest."""
        from datetime import timedelta

        from waldur_mastermind.marketplace.tasks import cleanup_old_software_catalogs

        now = timezone.now()

        # Create duplicate catalogs with same name/type but different versions
        older_duplicate = SoftwareCatalog.objects.create(
            name="Spack",
            version="2026.01.20",
            catalog_type="source_package",
            last_successful_update=now - timedelta(days=5),
        )
        oldest_duplicate = SoftwareCatalog.objects.create(
            name="Spack",
            version="2026.01.15",
            catalog_type="source_package",
            last_successful_update=now - timedelta(days=10),
        )

        # Now we have 3 Spack catalogs - self.recent_catalog is newest
        self.assertEqual(
            SoftwareCatalog.objects.filter(
                name="Spack", catalog_type="source_package"
            ).count(),
            3,
        )

        result = cleanup_old_software_catalogs()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["duplicates_deleted"], 2)

        # Only the newest Spack catalog should remain
        self.assertEqual(
            SoftwareCatalog.objects.filter(
                name="Spack", catalog_type="source_package"
            ).count(),
            1,
        )
        self.assertTrue(
            SoftwareCatalog.objects.filter(pk=self.recent_catalog.pk).exists()
        )
        self.assertFalse(SoftwareCatalog.objects.filter(pk=older_duplicate.pk).exists())
        self.assertFalse(
            SoftwareCatalog.objects.filter(pk=oldest_duplicate.pk).exists()
        )
