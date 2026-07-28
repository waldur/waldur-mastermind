from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.tests import factories


class OfferingForSubresourcesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(self.fixture.staff)

    def get_result(self, resource):
        url = factories.ResourceFactory.get_url(resource, "offering_for_subresources")
        return self.client.get(url)

    def test_resource_without_scope_returns_empty_list(self):
        # Regression: scope is None for unlinked/terminated resources and
        # offerings without backend integration; used to 500 with
        # AttributeError inside GenericKeyMixin.
        resource = factories.ResourceFactory(project=self.fixture.project)

        response = self.get_result(resource)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_offerings_of_service_settings_wrapping_resource_scope(self):
        scope_object = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(
            project=self.fixture.project, scope=scope_object
        )
        settings = structure_factories.ServiceSettingsFactory(scope=scope_object)
        child_offering = factories.OfferingFactory(scope=settings)

        response = self.get_result(resource)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            [{"uuid": child_offering.uuid.hex, "type": child_offering.type}],
        )

    def test_offerings_of_resource_scope_without_service_settings(self):
        scope_object = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(
            project=self.fixture.project, scope=scope_object
        )
        offering = factories.OfferingFactory(scope=scope_object)

        response = self.get_result(resource)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data, [{"uuid": offering.uuid.hex, "type": offering.type}]
        )

    def test_duplicate_service_settings_for_same_scope_do_not_crash(self):
        # ServiceSettings has no uniqueness constraint on (content_type,
        # object_id); .get(scope=...) would raise MultipleObjectsReturned.
        scope_object = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(
            project=self.fixture.project, scope=scope_object
        )
        structure_factories.ServiceSettingsFactory(scope=scope_object)
        structure_factories.ServiceSettingsFactory(scope=scope_object)

        response = self.get_result(resource)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
