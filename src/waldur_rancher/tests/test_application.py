from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests.factories import ProjectFactory

from . import factories, fixtures, utils


class ApplicationCreateTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()

    @utils.override_plugin_settings(READ_ONLY_MODE=True)
    def test_create_is_disabled_in_read_only_mode(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post('/api/rancher-apps/')
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @mock.patch('waldur_rancher.backend.RancherBackend.client')
    def test_create_is_enabled_for_owner(self, mock_client):
        self.client.force_authenticate(self.fixture.staff)

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

        mock_client.create_application.return_value = {'data': {}}

        response = self.client.post(
            '/api/rancher-apps/',
            {
                'service_settings': factories.RancherServiceSettingsFactory.get_url(
                    self.fixture.settings
                ),
                'project': ProjectFactory.get_url(self.fixture.project),
                'name': 'Test Catalog',
                'template': factories.TemplateFactory.get_url(template),
                'rancher_project': factories.ProjectFactory.get_url(project),
                'namespace': factories.NamespaceFactory.get_url(namespace),
                'version': '1.0',
                'answers': {},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
