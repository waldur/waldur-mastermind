from unittest.mock import patch

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models, tasks

from . import factories


class SoftwareCatalogModelTest(test.APITestCase):
    def setUp(self):
        self.catalog = factories.SoftwareCatalogFactory(
            name="EESSI",
            version="2023.06",
            source_url="https://software.eessi.io/",
            description="European Environment for Scientific Software Installations",
        )

    def test_catalog_str_representation(self):
        self.assertEqual(str(self.catalog), "EESSI 2023.06 (Binary Runtime (EESSI))")

    def test_catalog_has_uuid(self):
        self.assertIsNotNone(self.catalog.uuid)

    def test_catalog_has_timestamps(self):
        self.assertIsNotNone(self.catalog.created)
        self.assertIsNotNone(self.catalog.modified)


class SoftwarePackageModelTest(test.APITestCase):
    def setUp(self):
        self.catalog = factories.SoftwareCatalogFactory()
        self.package = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="Python",
            description="Python is a programming language",
            homepage="https://www.python.org/",
        )

    def test_package_str_representation(self):
        expected = f"Python ({self.catalog})"
        self.assertEqual(str(self.package), expected)

    def test_package_belongs_to_catalog(self):
        self.assertEqual(self.package.catalog, self.catalog)

    def test_package_has_uuid(self):
        self.assertIsNotNone(self.package.uuid)


class SoftwareVersionModelTest(test.APITestCase):
    def setUp(self):
        self.catalog = factories.SoftwareCatalogFactory()
        self.package = factories.SoftwarePackageFactory(catalog=self.catalog)
        self.version = factories.SoftwareVersionFactory(
            package=self.package, version="3.9.6"
        )

    def test_version_str_representation(self):
        self.assertEqual(str(self.version), f"{self.package.name} 3.9.6")

    def test_version_belongs_to_package(self):
        self.assertEqual(self.version.package, self.package)

    def test_version_has_uuid(self):
        self.assertIsNotNone(self.version.uuid)


class SoftwareTargetModelTest(test.APITestCase):
    def setUp(self):
        self.catalog = factories.SoftwareCatalogFactory()
        self.package = factories.SoftwarePackageFactory(catalog=self.catalog)
        self.version = factories.SoftwareVersionFactory(package=self.package)
        self.target = factories.SoftwareTargetFactory(
            version=self.version,
            target_type="cpu_architecture",
            target_name="x86_64",
            target_subtype="generic",
            location="/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic",
        )

    def test_target_str_representation(self):
        expected = f"{self.version} - {self.target.target_type}:{self.target.target_name}/{self.target.target_subtype}"
        self.assertEqual(str(self.target), expected)

    def test_target_belongs_to_version(self):
        self.assertEqual(self.target.version, self.version)

    def test_target_has_uuid(self):
        self.assertIsNotNone(self.target.uuid)


class OfferingSoftwareCatalogModelTest(test.APITestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.catalog = factories.SoftwareCatalogFactory()
        self.offering_catalog = factories.OfferingSoftwareCatalogFactory(
            offering=self.offering,
            catalog=self.catalog,
            enabled_cpu_family=["x86_64", "aarch64"],
            enabled_cpu_microarchitectures=["generic"],
        )

    def test_offering_catalog_str_representation(self):
        expected = f"{self.offering.name} - {self.catalog}"
        self.assertEqual(str(self.offering_catalog), expected)

    def test_offering_catalog_relationships(self):
        self.assertEqual(self.offering_catalog.offering, self.offering)
        self.assertEqual(self.offering_catalog.catalog, self.catalog)

    def test_offering_catalog_has_uuid(self):
        self.assertIsNotNone(self.offering_catalog.uuid)

    def test_enabled_targets_stored_as_list(self):
        self.assertIsInstance(self.offering_catalog.enabled_cpu_family, list)
        self.assertEqual(len(self.offering_catalog.enabled_cpu_family), 2)
        self.assertIsInstance(
            self.offering_catalog.enabled_cpu_microarchitectures, list
        )
        self.assertEqual(len(self.offering_catalog.enabled_cpu_microarchitectures), 1)


@ddt
class SoftwareCatalogViewSetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        self.url = factories.SoftwareCatalogFactory.get_list_url()

    @data("staff", "owner", "user", "customer_support", "admin", "manager")
    def test_catalog_list_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_anonymous_user_can_see_catalog_list(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_catalog_detail_includes_package_count(self):
        # Add some packages to test the count
        factories.SoftwarePackageFactory.create_batch(3, catalog=self.catalog)

        self.client.force_authenticate(self.fixture.staff)
        detail_url = factories.SoftwareCatalogFactory.get_url(self.catalog)
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["package_count"], 3)

    def test_filter_by_name(self):
        factories.SoftwareCatalogFactory(name="Other", version="1.0")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?name=EESSI")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "EESSI")

    def test_filter_by_version(self):
        factories.SoftwareCatalogFactory(name="EESSI", version="2024.01")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?version=2023.06")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["version"], "2023.06")

    @data("staff")
    def test_authorized_user_can_create_catalog(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "New Catalog",
            "version": "1.0",
            "source_url": "https://example.com",
            "description": "Test catalog",
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.SoftwareCatalog.objects.filter(name="New Catalog").exists()
        )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_unauthorized_user_cannot_create_catalog(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {"name": "New Catalog", "version": "1.0"}

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class SoftwarePackageViewSetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.catalog = factories.SoftwareCatalogFactory(version="0.5")
        self.package = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="Python",
            description="Python programming language",
        )
        self.url = factories.SoftwarePackageFactory.get_list_url()

    @data("staff", "owner", "user", "customer_support", "admin", "manager")
    def test_package_list_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_package_detail_includes_version_count(self):
        # Add some versions to test the count
        factories.SoftwareVersionFactory.create_batch(2, package=self.package)

        self.client.force_authenticate(self.fixture.staff)
        detail_url = factories.SoftwarePackageFactory.get_url(self.package)
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version_count"], 2)

    def test_filter_by_catalog(self):
        other_catalog = factories.SoftwareCatalogFactory()
        factories.SoftwarePackageFactory(catalog=other_catalog, name="Other Package")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + f"?catalog_uuid={self.catalog.uuid}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "Python")

    def test_search_by_name(self):
        factories.SoftwarePackageFactory(catalog=self.catalog, name="Java")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?name=Python")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "Python")

    def test_filter_by_exact_name(self):
        """Test that name_exact filter returns only exact name matches."""
        factories.SoftwarePackageFactory(catalog=self.catalog, name="R")
        factories.SoftwarePackageFactory(catalog=self.catalog, name="Ruby")
        factories.SoftwarePackageFactory(catalog=self.catalog, name="Rust")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?name_exact=R")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "R")

    def test_filter_by_offering_uuid(self):
        # Create offering with software catalog
        offering = factories.OfferingFactory()
        factories.OfferingSoftwareCatalogFactory(
            offering=offering, catalog=self.catalog
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + f"?offering_uuid={offering.uuid}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data("staff")
    def test_authorized_user_can_create_package(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "catalog": factories.SoftwareCatalogFactory.get_url(self.catalog),
            "name": "New Package",
            "description": "Test package",
            "homepage": "https://example.com",
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_unauthorized_user_cannot_create_package(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "catalog": factories.SoftwareCatalogFactory.get_url(self.catalog),
            "name": "New Package",
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_query_filter_by_name(self):
        """Test the new query filter searching by package name."""
        factories.SoftwarePackageFactory(catalog=self.catalog, name="Java")
        factories.SoftwarePackageFactory(catalog=self.catalog, name="Ruby")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?query=Python")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "Python")

    def test_query_filter_by_description(self):
        """Test the new query filter searching by description."""
        factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="Java",
            description="Object-oriented programming language",
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?query=programming")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            len(response.data), 2
        )  # Python and Java both have "programming" in description
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"Python", "Java"})

    def test_query_filter_by_version(self):
        """Test the new query filter searching by version."""
        package2 = factories.SoftwarePackageFactory(catalog=self.catalog, name="Java")
        factories.SoftwareVersionFactory(package=self.package, version="3.9.0")
        factories.SoftwareVersionFactory(package=self.package, version="3.10.0")
        factories.SoftwareVersionFactory(package=package2, version="17.0.2")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?query=3.9")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "Python")

    def test_nested_versions_in_package_detail(self):
        """Test that package detail includes nested versions with targets."""
        version1 = factories.SoftwareVersionFactory(
            package=self.package, version="3.9.0", release_date="2020-10-05"
        )
        version2 = factories.SoftwareVersionFactory(
            package=self.package, version="3.10.0", release_date="2021-10-04"
        )

        # Add targets to versions
        factories.SoftwareTargetFactory(
            version=version1, target_name="x86_64", target_subtype="generic"
        )
        factories.SoftwareTargetFactory(
            version=version1, target_name="aarch64", target_subtype="generic"
        )
        factories.SoftwareTargetFactory(
            version=version2, target_name="x86_64", target_subtype="generic"
        )

        self.client.force_authenticate(self.fixture.staff)
        detail_url = factories.SoftwarePackageFactory.get_url(self.package)
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("versions", response.data)
        self.assertEqual(len(response.data["versions"]), 2)

        # Check version structure
        version_data = response.data["versions"][0]
        self.assertIn("uuid", version_data)
        self.assertIn("version", version_data)
        self.assertIn("release_date", version_data)
        self.assertIn("targets", version_data)

        # Check targets structure (find version1 which has 2 targets)
        version1_data = next(
            v for v in response.data["versions"] if v["version"] == "3.9.0"
        )
        self.assertEqual(len(version1_data["targets"]), 2)
        target_data = version_data["targets"][0]
        self.assertIn("uuid", target_data)
        self.assertIn("target_name", target_data)
        self.assertIn("target_subtype", target_data)
        self.assertIn("location", target_data)

    def test_filter_by_catalog_version(self):
        """Test filtering packages by catalog version."""
        catalog2 = factories.SoftwareCatalogFactory(name="EESSI", version="2024.01")
        factories.SoftwarePackageFactory(catalog=catalog2, name="NewPackage")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?catalog_version=2024")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "NewPackage")

    def test_ordering_by_catalog_version(self):
        """Test ordering packages by catalog version."""
        catalog1 = factories.SoftwareCatalogFactory(name="Cat1", version="1.0")
        catalog2 = factories.SoftwareCatalogFactory(name="Cat2", version="2.0")
        catalog3 = factories.SoftwareCatalogFactory(name="Cat3", version="3.0")

        factories.SoftwarePackageFactory(catalog=catalog2, name="Pkg2")
        factories.SoftwarePackageFactory(catalog=catalog1, name="Pkg1")
        factories.SoftwarePackageFactory(catalog=catalog3, name="Pkg3")

        self.client.force_authenticate(self.fixture.staff)

        # Ascending order
        response = self.client.get(self.url + "?o=catalog_version")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [pkg["name"] for pkg in response.data]
        # First one is from setUp, then ordered by version
        self.assertEqual(names[1:], ["Pkg1", "Pkg2", "Pkg3"])

        # Descending order
        response = self.client.get(self.url + "?o=-catalog_version")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [pkg["name"] for pkg in response.data]
        self.assertEqual(names[:3], ["Pkg3", "Pkg2", "Pkg1"])


@ddt
class OfferingSoftwareCatalogActionsTest(test.APITestCase):
    """Test offering software catalog management actions."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")

        # Create some packages for the catalog
        self.package1 = factories.SoftwarePackageFactory(
            catalog=self.catalog, name="Python"
        )
        self.package2 = factories.SoftwarePackageFactory(
            catalog=self.catalog, name="Java"
        )

        self.base_url = factories.OfferingFactory.get_url(self.offering)

    @data("staff", "owner")
    def test_authorized_user_can_add_software_catalog(self, user):
        """Test adding a software catalog to an offering."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "catalog": str(self.catalog.uuid),
            "enabled_cpu_family": ["x86_64", "aarch64"],
            "enabled_cpu_microarchitectures": ["generic", "a64fx"],
        }

        url = self.base_url + "add_software_catalog/"
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("uuid", response.data)

        # Verify in database
        self.assertTrue(
            models.OfferingSoftwareCatalog.objects.filter(
                offering=self.offering, catalog=self.catalog
            ).exists()
        )

    @data("user", "admin", "manager")
    def test_unauthorized_project_user_cannot_add_software_catalog(self, user):
        """Test that unauthorized users cannot add software catalogs."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "catalog": str(self.catalog.uuid),
            "enabled_cpu_family": ["x86_64"],
        }

        url = self.base_url + "add_software_catalog/"
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("customer_support")
    def test_unauthorized_organization_user_cannot_add_software_catalog(self, user):
        """Test that unauthorized users cannot add software catalogs."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "catalog": str(self.catalog.uuid),
            "enabled_cpu_family": ["x86_64"],
        }

        url = self.base_url + "add_software_catalog/"
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_software_catalog_configuration(self):
        """Test updating software catalog configuration."""
        # First create the association
        offering_catalog = factories.OfferingSoftwareCatalogFactory(
            offering=self.offering,
            catalog=self.catalog,
            enabled_cpu_family=["x86_64"],
            enabled_cpu_microarchitectures=["generic"],
        )

        self.client.force_authenticate(self.fixture.staff)

        payload = {
            "enabled_cpu_family": ["x86_64", "aarch64", "arm64"],
            "enabled_cpu_microarchitectures": ["generic", "a64fx", "neoverse_n1"],
            "offering_catalog_uuid": offering_catalog.uuid.hex,
        }

        url = self.base_url + "update_software_catalog/"
        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["enabled_cpu_family"], ["x86_64", "aarch64", "arm64"]
        )
        self.assertEqual(
            response.data["enabled_cpu_microarchitectures"],
            ["generic", "a64fx", "neoverse_n1"],
        )

        # Verify in database
        offering_catalog.refresh_from_db()
        self.assertEqual(
            offering_catalog.enabled_cpu_family, ["x86_64", "aarch64", "arm64"]
        )
        self.assertEqual(
            offering_catalog.enabled_cpu_microarchitectures,
            ["generic", "a64fx", "neoverse_n1"],
        )

    def test_remove_software_catalog(self):
        """Test removing software catalog from offering."""
        # First create the association
        offering_catalog = factories.OfferingSoftwareCatalogFactory(
            offering=self.offering, catalog=self.catalog
        )

        payload = {
            "offering_catalog_uuid": offering_catalog.uuid.hex,
        }
        self.client.force_authenticate(self.fixture.staff)

        url = self.base_url + "remove_software_catalog/"
        response = self.client.post(url, payload)

        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        # Verify removed from database
        self.assertFalse(
            models.OfferingSoftwareCatalog.objects.filter(
                uuid=offering_catalog.uuid
            ).exists()
        )

    def test_offering_detail_includes_software_catalogs(self):
        """Test that offering detail includes associated software catalogs."""
        # Create associations
        factories.OfferingSoftwareCatalogFactory(
            offering=self.offering,
            catalog=self.catalog,
            enabled_cpu_family=["x86_64"],
            enabled_cpu_microarchitectures=["generic"],
        )

        catalog2 = factories.SoftwareCatalogFactory(name="Custom", version="1.0")
        factories.OfferingSoftwareCatalogFactory(
            offering=self.offering,
            catalog=catalog2,
            enabled_cpu_family=["aarch64"],
            enabled_cpu_microarchitectures=["a64fx"],
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.base_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("software_catalogs", response.data)
        self.assertEqual(len(response.data["software_catalogs"]), 2)

        # Check structure
        catalog_data = response.data["software_catalogs"][0]
        self.assertIn("uuid", catalog_data)
        self.assertIn("catalog", catalog_data)
        self.assertIn("enabled_cpu_family", catalog_data)
        self.assertIn("enabled_cpu_microarchitectures", catalog_data)

        # Check nested catalog info
        nested_catalog = catalog_data["catalog"]
        self.assertIn("uuid", nested_catalog)
        self.assertIn("name", nested_catalog)
        self.assertIn("version", nested_catalog)
        self.assertIn("description", nested_catalog)


class SoftwareVersionNewFieldsTest(test.APITestCase):
    """Test new fields in software version serializers (module, extensions, etc.)."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        self.package = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="TestPackage",
            description="Test package with EESSI metadata",
        )
        self.version = factories.SoftwareVersionFactory(
            package=self.package,
            version="1.0.0",
            metadata={
                "module": {
                    "full_module_name": "TestPackage/1.0.0-foss-2023b",
                    "module_name": "TestPackage",
                    "module_version": "1.0.0-foss-2023b",
                },
                "required_modules": [
                    {
                        "full_module_name": "EESSI/2023.06",
                        "module_name": "EESSI",
                        "module_version": "2023.06",
                    },
                    {
                        "full_module_name": "GCCcore/13.2.0",
                        "module_name": "GCCcore",
                        "module_version": "13.2.0",
                    },
                ],
                "extensions": [
                    {"type": "python", "name": "numpy", "version": "1.26.0"},
                    {"type": "python", "name": "scipy", "version": "1.11.0"},
                ],
                "toolchain": {"name": "foss", "version": "2023b"},
                "toolchain_families_compatibility": ["2023b_foss"],
            },
        )
        self.package_url = factories.SoftwarePackageFactory.get_url(self.package)
        self.version_list_url = factories.SoftwareVersionFactory.get_list_url()

    def test_nested_version_includes_module_field(self):
        """Test that nested version serializer includes module dict."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.package_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("versions", response.data)
        self.assertEqual(len(response.data["versions"]), 1)

        version_data = response.data["versions"][0]
        self.assertIn("module", version_data)
        self.assertIsInstance(version_data["module"], dict)
        self.assertEqual(
            version_data["module"]["full_module_name"], "TestPackage/1.0.0-foss-2023b"
        )
        self.assertEqual(version_data["module"]["module_name"], "TestPackage")
        self.assertEqual(version_data["module"]["module_version"], "1.0.0-foss-2023b")

    def test_nested_version_includes_required_modules_field(self):
        """Test that nested version serializer includes structured required_modules."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.package_url)

        version_data = response.data["versions"][0]
        self.assertIn("required_modules", version_data)
        self.assertIsInstance(version_data["required_modules"], list)
        self.assertEqual(len(version_data["required_modules"]), 2)

        # Check structure of required modules
        eessi_rm = next(
            rm
            for rm in version_data["required_modules"]
            if rm.get("module_name") == "EESSI"
        )
        self.assertEqual(eessi_rm["full_module_name"], "EESSI/2023.06")
        self.assertEqual(eessi_rm["module_version"], "2023.06")

    def test_nested_version_includes_extensions_field(self):
        """Test that nested version serializer includes extensions list."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.package_url)

        version_data = response.data["versions"][0]
        self.assertIn("extensions", version_data)
        self.assertIsInstance(version_data["extensions"], list)
        self.assertEqual(len(version_data["extensions"]), 2)

        # Verify extension structure
        ext_names = {ext.get("name") for ext in version_data["extensions"]}
        self.assertEqual(ext_names, {"numpy", "scipy"})

    def test_nested_version_includes_toolchain_field(self):
        """Test that nested version serializer includes toolchain dict."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.package_url)

        version_data = response.data["versions"][0]
        self.assertIn("toolchain", version_data)
        self.assertIsInstance(version_data["toolchain"], dict)
        self.assertEqual(version_data["toolchain"]["name"], "foss")
        self.assertEqual(version_data["toolchain"]["version"], "2023b")

    def test_nested_version_includes_toolchain_families_compatibility(self):
        """Test that nested version serializer includes toolchain_families_compatibility."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.package_url)

        version_data = response.data["versions"][0]
        self.assertIn("toolchain_families_compatibility", version_data)
        self.assertIsInstance(version_data["toolchain_families_compatibility"], list)
        self.assertIn("2023b_foss", version_data["toolchain_families_compatibility"])

    def test_version_detail_includes_new_fields(self):
        """Test that version detail serializer includes new EESSI fields."""
        self.client.force_authenticate(self.fixture.staff)
        version_url = factories.SoftwareVersionFactory.get_url(self.version)
        response = self.client.get(version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check all new fields are present
        self.assertIn("module", response.data)
        self.assertIn("required_modules", response.data)
        self.assertIn("extensions", response.data)
        self.assertIn("toolchain", response.data)
        self.assertIn("toolchain_families_compatibility", response.data)

        # Verify values
        self.assertEqual(response.data["module"]["module_name"], "TestPackage")
        self.assertEqual(len(response.data["extensions"]), 2)

    def test_empty_extensions_field(self):
        """Test that versions without extensions return empty list."""
        # Create version without extensions
        version_no_ext = factories.SoftwareVersionFactory(
            package=self.package,
            version="2.0.0",
            metadata={
                "module": {},
                "extensions": [],
                "required_modules": [],
            },
        )

        self.client.force_authenticate(self.fixture.staff)
        version_url = factories.SoftwareVersionFactory.get_url(version_no_ext)
        response = self.client.get(version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["extensions"], [])

    def test_missing_metadata_fields_return_empty(self):
        """Test that versions with missing metadata fields return empty values."""
        # Create version with minimal metadata
        version_minimal = factories.SoftwareVersionFactory(
            package=self.package,
            version="3.0.0",
            metadata={},  # Empty metadata
        )

        self.client.force_authenticate(self.fixture.staff)
        version_url = factories.SoftwareVersionFactory.get_url(version_minimal)
        response = self.client.get(version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["module"], {})
        self.assertEqual(response.data["required_modules"], [])
        self.assertEqual(response.data["extensions"], [])
        self.assertEqual(response.data["toolchain"], {})
        self.assertEqual(response.data["toolchain_families_compatibility"], [])


class SoftwarePackageExtensionFilterTest(test.APITestCase):
    """Test extension filtering for software packages."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")

        # Package with Python extensions
        self.pkg_with_python_ext = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="PackageWithPython",
        )
        factories.SoftwareVersionFactory(
            package=self.pkg_with_python_ext,
            version="1.0.0",
            metadata={
                "extensions": [
                    {"type": "python", "name": "numpy", "version": "1.26.0"},
                    {"type": "python", "name": "scipy", "version": "1.11.0"},
                ],
            },
        )

        # Package with component extensions
        self.pkg_with_component_ext = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="PackageWithComponent",
        )
        factories.SoftwareVersionFactory(
            package=self.pkg_with_component_ext,
            version="2.0.0",
            metadata={
                "extensions": [
                    {"type": "component", "name": "cuda-bindings", "version": "12.0"},
                ],
            },
        )

        # Package without extensions
        self.pkg_no_extensions = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="PackageNoExtensions",
        )
        factories.SoftwareVersionFactory(
            package=self.pkg_no_extensions,
            version="1.0.0",
            metadata={"extensions": []},
        )

        self.url = factories.SoftwarePackageFactory.get_list_url()

    def test_filter_by_extension_type_python(self):
        """Test filtering packages by extension type 'python'."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?extension_type=python")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"PackageWithPython"})

    def test_filter_by_extension_type_component(self):
        """Test filtering packages by extension type 'component'."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?extension_type=component")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"PackageWithComponent"})

    def test_filter_by_extension_name(self):
        """Test filtering packages by extension name."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?extension_name=numpy")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"PackageWithPython"})

    def test_filter_by_extension_name_cuda(self):
        """Test filtering packages by extension name 'cuda-bindings'."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?extension_name=cuda-bindings")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"PackageWithComponent"})

    def test_filter_by_nonexistent_extension_type(self):
        """Test filtering by non-existent extension type returns empty result."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?extension_type=nonexistent")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_filter_by_nonexistent_extension_name(self):
        """Test filtering by non-existent extension name returns empty result."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?extension_name=nonexistent")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class SoftwarePackageIsExtensionFilterTest(test.APITestCase):
    """Test is_extension filtering for software packages."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")

        self.parent_package = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="ParentPackage",
            is_extension=False,
        )
        self.extension_package = factories.SoftwarePackageFactory(
            catalog=self.catalog,
            name="ExtensionPackage",
            is_extension=True,
            parent_software=self.parent_package,
        )
        self.url = factories.SoftwarePackageFactory.get_list_url()

    def test_filter_extensions_only(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?is_extension=true")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"ExtensionPackage"})

    def test_filter_non_extensions_only(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?is_extension=false")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"ParentPackage"})

    def test_no_filter_returns_all(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        package_names = {pkg["name"] for pkg in response.data}
        self.assertEqual(package_names, {"ParentPackage", "ExtensionPackage"})


@ddt
class SoftwareCatalogDiscoverTest(test.APITestCase):
    """Tests for the discover endpoint on SoftwareCatalogViewSet."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.SoftwareCatalogFactory.get_list_url() + "discover/"

    @patch(
        "waldur_mastermind.marketplace.views.detect_eessi_version",
        return_value="2025.06",
    )
    @patch(
        "waldur_mastermind.marketplace.views.detect_spack_version",
        return_value="2026.01.15",
    )
    def test_staff_can_discover_versions(self, mock_spack, mock_eessi):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        eessi = next(e for e in response.data if e["name"] == "EESSI")
        spack = next(e for e in response.data if e["name"] == "Spack")

        self.assertEqual(eessi["latest_version"], "2025.06")
        self.assertFalse(eessi["existing"])
        self.assertIsNone(eessi["existing_version"])
        self.assertFalse(eessi["update_available"])

        self.assertEqual(spack["latest_version"], "2026.01.15")
        self.assertFalse(spack["existing"])

    @patch(
        "waldur_mastermind.marketplace.views.detect_eessi_version",
        return_value="2025.06",
    )
    @patch(
        "waldur_mastermind.marketplace.views.detect_spack_version",
        return_value="2026.01.15",
    )
    def test_discover_shows_existing_catalog_info(self, mock_spack, mock_eessi):
        # Create an existing EESSI catalog with an older version
        factories.SoftwareCatalogFactory(
            name="EESSI", version="2024.01", catalog_type="binary_runtime"
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        eessi = next(e for e in response.data if e["name"] == "EESSI")
        self.assertTrue(eessi["existing"])
        self.assertEqual(eessi["existing_version"], "2024.01")
        self.assertTrue(eessi["update_available"])

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_non_staff_cannot_discover(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_discover(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch(
        "waldur_mastermind.marketplace.views.detect_eessi_version",
        side_effect=Exception("Network error"),
    )
    @patch(
        "waldur_mastermind.marketplace.views.detect_spack_version",
        return_value="2026.01.15",
    )
    def test_discover_handles_detection_failure_gracefully(
        self, mock_spack, mock_eessi
    ):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        eessi = next(e for e in response.data if e["name"] == "EESSI")
        self.assertIsNone(eessi["latest_version"])
        self.assertFalse(eessi["update_available"])

        spack = next(e for e in response.data if e["name"] == "Spack")
        self.assertEqual(spack["latest_version"], "2026.01.15")


@ddt
class SoftwareCatalogImportTest(test.APITestCase):
    """Tests for the import_catalog action on SoftwareCatalogViewSet."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.SoftwareCatalogFactory.get_list_url() + "import_catalog/"

    @patch.object(tasks.import_software_catalog, "delay")
    def test_staff_can_import_catalog(self, mock_delay):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"name": "EESSI"})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "importing")
        self.assertEqual(response.data["name"], "EESSI")
        mock_delay.assert_called_once_with("EESSI", "binary_runtime")

    @patch.object(tasks.import_software_catalog, "delay")
    def test_import_duplicate_catalog_fails(self, mock_delay):
        factories.SoftwareCatalogFactory(name="EESSI", catalog_type="binary_runtime")

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"name": "EESSI"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_delay.assert_not_called()

    def test_import_unknown_catalog_fails(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"name": "Unknown"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_non_staff_cannot_import(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, {"name": "EESSI"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_import(self):
        response = self.client.post(self.url, {"name": "EESSI"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class SoftwareCatalogUpdateTest(test.APITestCase):
    """Tests for the update_catalog action on SoftwareCatalogViewSet."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.catalog = factories.SoftwareCatalogFactory(
            name="EESSI", version="2023.06", catalog_type="binary_runtime"
        )
        self.url = (
            factories.SoftwareCatalogFactory.get_url(self.catalog) + "update_catalog/"
        )

    @patch.object(tasks.update_single_software_catalog, "delay")
    def test_staff_can_update_catalog(self, mock_delay):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "updating")
        self.assertEqual(response.data["catalog_uuid"], str(self.catalog.uuid))
        mock_delay.assert_called_once_with(self.catalog.uuid.hex)

    @patch.object(tasks.update_single_software_catalog, "delay")
    def test_update_unknown_loader_fails(self, mock_delay):
        catalog = factories.SoftwareCatalogFactory(
            name="CustomCatalog", version="1.0", catalog_type="binary_runtime"
        )
        url = factories.SoftwareCatalogFactory.get_url(catalog) + "update_catalog/"

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_delay.assert_not_called()

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_non_staff_cannot_update(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_update(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
