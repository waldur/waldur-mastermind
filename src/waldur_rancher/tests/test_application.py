from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests.factories import ProjectFactory
from waldur_rancher import models

from . import factories, fixtures, utils


@ddt
class ApplicationCreateTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_create_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post("/api/rancher-apps/")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @data("staff", "owner", "admin", "manager")
    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_who_manages_the_project_can_create(self, user, mock_client):
        mock_client.create_application.return_value = {"data": {}}
        response = self.post_application(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("member", "customer_support", "global_support")
    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_who_only_sees_the_project_cannot_create(self, user, mock_client):
        response = self.post_application(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_client.create_application.assert_not_called()

    @mock.patch("waldur_rancher.backend.RancherBackend.client")
    def test_refused_create_leaves_no_namespace_behind(self, mock_client):
        # Validation creates a namespace named in the request; a refused
        # create must not keep it.
        response = self.post_application(
            self.fixture.member, namespace_name="left-behind"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Namespace.objects.filter(name="left-behind").exists())

    def post_application(self, user, namespace_name=None):
        self.client.force_authenticate(user)

        catalog = factories.CatalogFactory(settings=self.fixture.settings)
        project = factories.ProjectFactory(
            settings=self.fixture.settings, cluster=self.fixture.cluster
        )
        template = factories.TemplateFactory(
            settings=self.fixture.settings, catalog=catalog
        )
        namespace = factories.NamespaceFactory(
            settings=self.fixture.settings, project=project
        )

        return self.client.post(
            "/api/rancher-apps/",
            {
                "service_settings": factories.RancherServiceSettingsFactory.get_url(
                    self.fixture.settings
                ),
                "project": ProjectFactory.get_url(self.fixture.project),
                "name": "Test Catalog",
                "template": factories.TemplateFactory.get_url(template),
                "rancher_project": factories.ProjectFactory.get_url(project),
                **(
                    {"namespace_name": namespace_name}
                    if namespace_name
                    else {"namespace": factories.NamespaceFactory.get_url(namespace)}
                ),
                "version": "1.0",
                "answers": {},
            },
        )
