import json
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework import test

from waldur_mastermind.marketplace import models

from . import factories


class LoadEessiCatalogCommandTest(test.APITransactionTestCase):
    def setUp(self):
        self.sample_eessi_data = {
            "targets": [
                "/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic",
                "/cvmfs/software.eessi.io/versions/2023.06/software/linux/aarch64/generic",
            ],
            "software": {
                "Python": {
                    "description": "Python is a programming language",
                    "homepage": "https://www.python.org/",
                    "versions": {
                        "3.9.6": {"versionsuffix": "", "toolchain": "GCCcore/11.2.0"},
                        "3.10.4": {"versionsuffix": "", "toolchain": "GCCcore/11.3.0"},
                    },
                },
                "GCC": {
                    "description": "GNU Compiler Collection",
                    "homepage": "https://gcc.gnu.org/",
                    "versions": {"11.2.0": {"versionsuffix": "", "toolchain": ""}},
                },
            },
        }

    def create_temp_json_file(self, data=None):
        """Create a temporary JSON file with EESSI data."""
        if data is None:
            data = self.sample_eessi_data

        temp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, temp_file)
        temp_file.close()
        return temp_file.name

    def test_dry_run_shows_what_would_be_done(self):
        """Test dry run mode shows correct information without making changes."""
        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command(
            "load_eessi_catalog", "--json-file", json_file, "--dry-run", stdout=out
        )

        output = out.getvalue()
        self.assertIn("DRY RUN - No changes will be made", output)
        self.assertIn("Would create/update software catalog: EESSI 2023.06", output)
        self.assertIn("Would process 2 software packages", output)
        self.assertIn("Would process 3 software versions", output)
        self.assertIn("Would process 6 software targets", output)
        self.assertIn(
            "SYNC ENABLED: Missing packages/versions/targets will be DELETED", output
        )
        self.assertIn("Detected architectures: aarch64, x86_64", output)

        # Verify no actual data was created
        self.assertEqual(models.SoftwareCatalog.objects.count(), 0)

    def test_dry_run_with_no_sync(self):
        """Test dry run with sync disabled."""
        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command(
            "load_eessi_catalog",
            "--json-file",
            json_file,
            "--dry-run",
            "--no-sync",
            stdout=out,
        )

        output = out.getvalue()
        self.assertIn("Sync disabled: Existing records will be preserved", output)
        self.assertNotIn("SYNC ENABLED", output)

    def test_load_catalog_creates_models(self):
        """Test that loading creates all necessary models."""
        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command("load_eessi_catalog", "--json-file", json_file, stdout=out)

        # Check catalog was created
        self.assertEqual(models.SoftwareCatalog.objects.count(), 1)
        catalog = models.SoftwareCatalog.objects.first()
        self.assertEqual(catalog.name, "EESSI")
        self.assertEqual(catalog.version, "2023.06")
        self.assertEqual(catalog.source_url, "https://software.eessi.io/")

        # Check packages were created
        self.assertEqual(models.SoftwarePackage.objects.count(), 2)
        python_package = models.SoftwarePackage.objects.get(name="Python")
        self.assertEqual(python_package.description, "Python is a programming language")
        self.assertEqual(python_package.homepage, "https://www.python.org/")

        # Check versions were created
        self.assertEqual(models.SoftwareVersion.objects.count(), 3)
        python_versions = models.SoftwareVersion.objects.filter(package=python_package)
        self.assertEqual(python_versions.count(), 2)

        # Check targets were created (2 architectures × 3 versions = 6 targets)
        self.assertEqual(models.SoftwareTarget.objects.count(), 6)

        # Verify output
        output = out.getvalue()
        self.assertIn("EESSI catalog loaded successfully", output)
        self.assertIn("Packages created: 2", output)
        self.assertIn("Versions created: 3", output)
        self.assertIn("Targets created: 6", output)

    def test_load_catalog_with_custom_name_and_version(self):
        """Test loading catalog with custom name and version."""
        json_file = self.create_temp_json_file()

        call_command(
            "load_eessi_catalog",
            "--json-file",
            json_file,
            "--catalog-name",
            "Custom Software",
            "--catalog-version",
            "1.0",
        )

        catalog = models.SoftwareCatalog.objects.first()
        self.assertEqual(catalog.name, "Custom Software")
        self.assertEqual(catalog.version, "1.0")

    def test_update_existing_catalog(self):
        """Test updating an existing catalog."""
        # Create initial catalog with different description
        from waldur_mastermind.marketplace.models import SoftwareCatalog

        initial_catalog = SoftwareCatalog.objects.create(
            name="EESSI",
            version="2023.06",
            description="Old description",
            source_url="https://old-url.com",
        )
        initial_description = initial_catalog.description
        initial_url = initial_catalog.source_url

        # Update with management command
        json_file = self.create_temp_json_file()
        out = StringIO()
        call_command(
            "load_eessi_catalog",
            "--json-file",
            json_file,
            "--update-existing",
            stdout=out,
        )

        # Check catalog was updated
        updated_catalog = SoftwareCatalog.objects.first()
        self.assertEqual(updated_catalog.uuid, initial_catalog.uuid)
        # Description and URL should be updated to the standard EESSI values
        self.assertNotEqual(updated_catalog.description, initial_description)
        self.assertNotEqual(updated_catalog.source_url, initial_url)
        self.assertEqual(updated_catalog.source_url, "https://software.eessi.io/")

        output = out.getvalue()
        self.assertIn("Updated existing software catalog", output)

    def test_sync_removes_missing_packages(self):
        """Test that sync removes packages not in JSON."""
        # Create catalog with extra package
        catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        extra_package = factories.SoftwarePackageFactory(
            catalog=catalog, name="ExtraPackage"
        )
        extra_version = factories.SoftwareVersionFactory(package=extra_package)
        factories.SoftwareTargetFactory(version=extra_version)

        # Load EESSI data (which doesn't include ExtraPackage)
        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command(
            "load_eessi_catalog",
            "--json-file",
            json_file,
            "--update-existing",
            stdout=out,
        )

        # Check that extra package was removed
        self.assertFalse(
            models.SoftwarePackage.objects.filter(name="ExtraPackage").exists()
        )

        # Check that EESSI packages are still there
        self.assertTrue(models.SoftwarePackage.objects.filter(name="Python").exists())
        self.assertTrue(models.SoftwarePackage.objects.filter(name="GCC").exists())

        output = out.getvalue()
        self.assertIn("Packages deleted: 1", output)

    def test_no_sync_preserves_extra_packages(self):
        """Test that no-sync preserves extra packages."""
        # Create catalog with extra package
        catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        factories.SoftwarePackageFactory(catalog=catalog, name="ExtraPackage")

        # Load EESSI data with no-sync
        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command(
            "load_eessi_catalog",
            "--json-file",
            json_file,
            "--update-existing",
            "--no-sync",
            stdout=out,
        )

        # Check that extra package was preserved
        self.assertTrue(
            models.SoftwarePackage.objects.filter(name="ExtraPackage").exists()
        )

        # Check that EESSI packages are also there
        self.assertTrue(models.SoftwarePackage.objects.filter(name="Python").exists())

        output = out.getvalue()
        # Should not mention any deletions
        self.assertNotIn("deleted:", output)

    def test_version_extraction_from_targets(self):
        """Test automatic version extraction from target paths."""
        data_with_version = {
            "targets": [
                "/cvmfs/software.eessi.io/versions/2024.01/software/linux/x86_64/generic"
            ],
            "software": {
                "TestPackage": {
                    "description": "Test package",
                    "homepage": "https://example.com",
                    "versions": {
                        "1.0": {"versionsuffix": "", "toolchain": "GCC/11.2.0"}
                    },
                }
            },
        }

        json_file = self.create_temp_json_file(data_with_version)

        call_command("load_eessi_catalog", "--json-file", json_file)

        catalog = models.SoftwareCatalog.objects.first()
        self.assertEqual(catalog.version, "2024.01")

    def test_architecture_extraction_from_targets(self):
        """Test automatic architecture extraction from target paths."""
        data_with_multiple_archs = {
            "targets": [
                "/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic",
                "/cvmfs/software.eessi.io/versions/2023.06/software/linux/aarch64/generic",
                "/cvmfs/software.eessi.io/versions/2023.06/software/linux/ppc64le/generic",
            ],
            "software": {
                "TestPackage": {
                    "description": "Test package",
                    "homepage": "https://example.com",
                    "versions": {"1.0": {"versionsuffix": "", "toolchain": ""}},
                }
            },
        }

        json_file = self.create_temp_json_file(data_with_multiple_archs)

        out = StringIO()
        call_command(
            "load_eessi_catalog", "--json-file", json_file, "--dry-run", stdout=out
        )

        output = out.getvalue()
        self.assertIn("Detected architectures: aarch64, ppc64le, x86_64", output)

    def test_error_handling_invalid_json(self):
        """Test error handling for invalid JSON files."""
        # Create file with invalid JSON
        temp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        temp_file.write("{ invalid json")
        temp_file.close()

        with self.assertRaises(CommandError):
            call_command("load_eessi_catalog", "--json-file", temp_file.name)

    def test_error_handling_missing_file(self):
        """Test error handling for missing JSON files."""
        with self.assertRaises(CommandError):
            call_command("load_eessi_catalog", "--json-file", "/nonexistent/file.json")

    def test_catalog_already_exists_without_update_flag(self):
        """Test behavior when catalog exists but update flag is not set."""
        # Create existing catalog
        factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")

        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command("load_eessi_catalog", "--json-file", json_file, stdout=out)

        output = out.getvalue()
        self.assertIn(
            "Software catalog already exists. Use --update-existing to update it.",
            output,
        )

        # Should still have only the original catalog
        self.assertEqual(models.SoftwareCatalog.objects.count(), 1)
        self.assertEqual(models.SoftwarePackage.objects.count(), 0)

    def test_dry_run_shows_packages_to_remove(self):
        """Test that dry run shows packages that would be removed."""
        # Create catalog with packages
        catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        factories.SoftwarePackageFactory(catalog=catalog, name="OldPackage")
        factories.SoftwarePackageFactory(catalog=catalog, name="AnotherOldPackage")

        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command(
            "load_eessi_catalog", "--json-file", json_file, "--dry-run", stdout=out
        )

        output = out.getvalue()
        self.assertIn("Would remove 2 packages not in JSON", output)

    def test_sample_packages_shown_in_dry_run(self):
        """Test that dry run shows sample packages from JSON."""
        json_file = self.create_temp_json_file()

        out = StringIO()
        call_command(
            "load_eessi_catalog", "--json-file", json_file, "--dry-run", stdout=out
        )

        output = out.getvalue()
        self.assertIn("Sample software packages:", output)
        self.assertIn("- Python (2 versions)", output)
        self.assertIn("- GCC (1 versions)", output)
