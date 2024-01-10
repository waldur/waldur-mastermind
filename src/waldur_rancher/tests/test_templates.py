from rest_framework import status, test

from waldur_rancher.tests import factories, fixtures


class TemplatesFilterTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.url = factories.TemplateFactory.get_list_url()

    def test_global_templates_should_be_included_when_cluster_level_filtering_is_used(
        self,
    ):
        # Arrange
        global_catalog = factories.CatalogFactory(scope=self.fixture.settings)
        global_template = factories.TemplateFactory(catalog=global_catalog)
        cluster_template = factories.TemplateFactory(cluster=self.fixture.cluster)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, data={"cluster_uuid": self.fixture.cluster.uuid.hex}
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        template_ids = [template["uuid"] for template in response.data]
        self.assertTrue(global_template.uuid.hex in template_ids)
        self.assertTrue(cluster_template.uuid.hex in template_ids)
