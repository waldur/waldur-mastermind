from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher.const import DEPLOYMENT_MODE_MANAGED
from waldur_rancher import models

from . import factories, fixtures, utils


class CatalogCreateTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.CatalogFactory.get_list_url()

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_create_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_create_is_enabled_for_owner(self, mock_backend):
        mock_backend.create_cluster_catalog.return_value = {"id": "1", "state": "ok"}
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url,
            {
                "name": "Test Catalog",
                "catalog_url": "http://example.com/",
                "branch": "master",
                "scope": factories.ClusterFactory.get_url(self.fixture.cluster),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class CatalogUpdateTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.catalog = factories.CatalogFactory(scope=self.fixture.cluster)
        self.url = factories.CatalogFactory.get_url(self.catalog)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_update_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"name": "New name"})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_update_is_enabled_for_owner(self, mock_backend):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"name": "New name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CatalogDeleteTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.catalog = factories.CatalogFactory(scope=self.fixture.cluster)
        self.url = factories.CatalogFactory.get_url(self.catalog)

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_delete_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_delete_is_enabled_for_owner(self, mock_backend):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_delete_catalog_if_related_cluster_has_been_deleted(self):
        self.catalog.scope.delete()
        self.assertRaises(models.Catalog.DoesNotExist, self.catalog.refresh_from_db)

    def test_delete_catalog_if_related_settings_has_been_deleted(self):
        self.catalog.scope = factories.RancherServiceSettingsFactory()
        self.catalog.save()
        self.catalog.scope.delete()
        self.assertRaises(models.Catalog.DoesNotExist, self.catalog.refresh_from_db)

    def test_delete_catalog_if_related_project_has_been_deleted(self):
        self.catalog.scope = factories.ProjectFactory()
        self.catalog.save()
        self.catalog.scope.delete()
        self.assertRaises(models.Catalog.DoesNotExist, self.catalog.refresh_from_db)


@mock.patch("waldur_rancher.backend.RancherBackend.client")
class GlobalCatalogPermissionTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.catalog = factories.CatalogFactory(scope=self.fixture.settings)
        self.url = factories.CatalogFactory.get_url(self.catalog)

    def test_owner_cannot_create_global_catalog(self, mock_client):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            factories.CatalogFactory.get_list_url(),
            {
                "name": "Global catalog",
                "catalog_url": "http://example.com/",
                "branch": "master",
                "scope": structure_factories.ServiceSettingsFactory.get_url(
                    self.fixture.settings
                ),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_client.create_global_catalog.assert_not_called()

    def test_owner_cannot_update_global_catalog(self, mock_client):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"catalog_url": "http://example.org/"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_client.update_global_catalog.assert_not_called()

    def test_owner_cannot_delete_global_catalog(self, mock_client):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_client.delete_global_catalog.assert_not_called()
        self.catalog.refresh_from_db()

    def test_staff_can_update_global_catalog(self, mock_client):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {"catalog_url": "http://example.org/"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_catalogs_of_settings_hidden_from_user_are_not_listed(self, mock_client):
        hidden_settings = factories.RancherServiceSettingsFactory(shared=False)
        factories.CatalogFactory(scope=hidden_settings)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            factories.CatalogFactory.get_list_url(),
            {"settings_uuid": hidden_settings.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@mock.patch("waldur_rancher.backend.RancherBackend.client")
class ScopedCatalogPermissionTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()

    def test_project_member_cannot_update_cluster_catalog(self, mock_client):
        catalog = factories.CatalogFactory(scope=self.fixture.cluster)
        self.client.force_authenticate(self.fixture.member)
        response = self.client.patch(
            factories.CatalogFactory.get_url(catalog), {"name": "New name"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_client.update_cluster_catalog.assert_not_called()

    def test_project_admin_can_update_cluster_catalog(self, mock_client):
        catalog = factories.CatalogFactory(scope=self.fixture.cluster)
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.patch(
            factories.CatalogFactory.get_url(catalog), {"name": "New name"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_member_cannot_delete_project_catalog(self, mock_client):
        rancher_project = factories.ProjectFactory(
            cluster=self.fixture.cluster, settings=self.fixture.settings
        )
        catalog = factories.CatalogFactory(scope=rancher_project)
        self.client.force_authenticate(self.fixture.member)
        response = self.client.delete(factories.CatalogFactory.get_url(catalog))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_client.delete_project_catalog.assert_not_called()

    def test_project_admin_can_update_project_catalog(self, mock_client):
        rancher_project = factories.ProjectFactory(
            cluster=self.fixture.cluster, settings=self.fixture.settings
        )
        catalog = factories.CatalogFactory(scope=rancher_project)
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.patch(
            factories.CatalogFactory.get_url(catalog), {"name": "New name"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_update_cluster_catalog(self, mock_client):
        catalog = factories.CatalogFactory(scope=self.fixture.cluster)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.CatalogFactory.get_url(catalog), {"name": "New name"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_delete_catalog_of_project_without_cluster(self, mock_client):
        rancher_project = factories.ProjectFactory(
            cluster=None, settings=self.fixture.settings
        )
        catalog = factories.CatalogFactory(scope=rancher_project)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(factories.CatalogFactory.get_url(catalog))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_project_admin_cannot_update_project_catalog_of_managed_cluster(
        self, mock_client
    ):
        offering = marketplace_factories.OfferingFactory(
            plugin_options={"deployment_mode": DEPLOYMENT_MODE_MANAGED}
        )
        marketplace_factories.ResourceFactory(
            scope=self.fixture.cluster, offering=offering
        )
        rancher_project = factories.ProjectFactory(
            cluster=self.fixture.cluster, settings=self.fixture.settings
        )
        catalog = factories.CatalogFactory(scope=rancher_project)
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.patch(
            factories.CatalogFactory.get_url(catalog), {"name": "New name"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
