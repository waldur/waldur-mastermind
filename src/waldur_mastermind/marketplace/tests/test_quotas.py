from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import serializers as structure_test_serializers
from waldur_core.structure.tests import views as structure_test_views
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories, utils


class NewInstanceSerializer(structure_test_serializers.NewInstanceSerializer):
    class Meta(structure_test_serializers.NewInstanceSerializer.Meta):
        read_only_fields = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["service_settings"].validate_quota_change({"cores": attrs["cores"]})
        return attrs


class TestNewInstanceViewSet(structure_test_views.TestNewInstanceViewSet):
    serializer_class = NewInstanceSerializer


class TestNewInstanceCreateProcessor(utils.TestCreateProcessor):
    viewset = TestNewInstanceViewSet
    fields = ["name", "cores"]


class QuotasValidateTest(test.APITransactionTestCase):
    def setUp(self):
        manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=TestNewInstanceCreateProcessor,
        )
        self.service_settings = structure_factories.ServiceSettingsFactory(
            type="Test", shared=True
        )
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            type="TEST_TYPE",
            scope=self.service_settings,
        )

        self.service_settings.set_quota_limit("cores", 1)

    def test_order_created_if_quotas_are_valid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.OrderFactory.get_list_url(),
            {
                "offering": factories.OfferingFactory.get_public_url(self.offering),
                "project": structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                "attributes": {"name": "test", "cores": 1},
                "plan": factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=self.offering)
                ),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_order_is_not_created_if_quotas_are_invalid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.OrderFactory.get_list_url(),
            {
                "offering": factories.OfferingFactory.get_public_url(self.offering),
                "project": structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                "attributes": {"name": "test", "cores": 2},
                "plan": factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=self.offering)
                ),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            "One or more quotas were exceeded: cores quota limit: 1, requires 2"
            in response.data["non_field_errors"][0]
        )
