from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models

from . import factories


class SoftwareCatalogModelTest(test.APITransactionTestCase):
    def setUp(self):
        self.catalog = factories.SoftwareCatalogFactory(
            name="EESSI",
            version="2023.06",
            source_url="https://software.eessi.io/",
            description="European Environment for Scientific Software Installations",
        )

    def test_catalog_str_representation(self):
        self.assertEqual(str(self.catalog), "EESSI 2023.06")

    def test_catalog_has_uuid(self):
        self.assertIsNotNone(self.catalog.uuid)

    def test_catalog_has_timestamps(self):
        self.assertIsNotNone(self.catalog.created)
        self.assertIsNotNone(self.catalog.modified)


class SoftwarePackageModelTest(test.APITransactionTestCase):
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


class SoftwareVersionModelTest(test.APITransactionTestCase):
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


class SoftwareTargetModelTest(test.APITransactionTestCase):
    def setUp(self):
        self.catalog = factories.SoftwareCatalogFactory()
        self.package = factories.SoftwarePackageFactory(catalog=self.catalog)
        self.version = factories.SoftwareVersionFactory(package=self.package)
        self.target = factories.SoftwareTargetFactory(
            version=self.version,
            cpu_family="x86_64",
            cpu_microarchitecture="generic",
            path="/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic",
        )

    def test_target_str_representation(self):
        expected = f"{self.version} - {self.target.cpu_family}/{self.target.cpu_microarchitecture}"
        self.assertEqual(str(self.target), expected)

    def test_target_belongs_to_version(self):
        self.assertEqual(self.target.version, self.version)

    def test_target_has_uuid(self):
        self.assertIsNotNone(self.target.uuid)


class OfferingSoftwareCatalogModelTest(test.APITransactionTestCase):
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
        expected = f"{self.offering.name} - {self.catalog.name} {self.catalog.version}"
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
class SoftwareCatalogViewSetTest(test.APITransactionTestCase):
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
class SoftwarePackageViewSetTest(test.APITransactionTestCase):
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
            version=version1, cpu_family="x86_64", cpu_microarchitecture="generic"
        )
        factories.SoftwareTargetFactory(
            version=version1, cpu_family="aarch64", cpu_microarchitecture="generic"
        )
        factories.SoftwareTargetFactory(
            version=version2, cpu_family="x86_64", cpu_microarchitecture="generic"
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
        self.assertIn("cpu_family", target_data)
        self.assertIn("cpu_microarchitecture", target_data)
        self.assertIn("path", target_data)

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
class OfferingSoftwareCatalogActionsTest(test.APITransactionTestCase):
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
