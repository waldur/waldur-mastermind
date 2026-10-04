from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums, handlers, models

from . import factories

DEFINITIONS = "/api/marketplace-metric-definitions/"
OFFERING_METRICS = "/api/marketplace-offering-metrics/"


class MetricDefinitionTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        self.fixture = structure_fixtures.ProjectFixture()
        self.payload = {
            "key": "support.tickets.resolved",
            "name": "Tickets resolved",
            "unit": "{tickets}",
            "kind": enums.MetricKinds.COUNTER,
            "good_direction": enums.GoodDirections.UP,
            "attribute_keys": ["queue"],
        }

    def create(self, user, **extra):
        self.client.force_authenticate(user)
        return self.client.post(DEFINITIONS, {**self.payload, **extra}, format="json")

    def test_staff_creates_a_global_definition(self):
        response = self.create(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(models.MetricDefinition.objects.get().owner_customer)

    def test_service_provider_manager_creates_and_sees_a_private_definition(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.CREATE_OFFERING)
        provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        manager = structure_factories.UserFactory()
        provider.add_user(manager, ServiceProviderRole.MANAGER)

        response = self.create(manager, owner_customer=self.fixture.customer.uuid.hex)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        listed = self.client.get(DEFINITIONS).data
        self.assertIn(response.data["uuid"], {item["uuid"] for item in listed})

    def test_provider_owner_creates_a_private_definition(self):
        response = self.create(
            self.fixture.owner, owner_customer=self.fixture.customer.uuid.hex
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_provider_owner_cannot_create_a_global_definition(self):
        response = self.create(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_resource_is_a_reserved_attribute_name(self):
        response = self.create(self.fixture.staff, attribute_keys=["resource"])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attribute_keys", response.data)

    def test_outsider_cannot_create_a_private_definition_for_a_provider(self):
        response = self.create(
            structure_factories.UserFactory(),
            owner_customer=self.fixture.customer.uuid.hex,
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_key_is_unique_among_global_definitions(self):
        factories.MetricDefinitionFactory(key="support.tickets.resolved")

        response = self.create(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_malformed_key_is_refused(self):
        response = self.create(self.fixture.staff, key="Tickets Resolved")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_key_cannot_change(self):
        definition = factories.MetricDefinitionFactory()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.patch(
            factories.MetricDefinitionFactory.get_url(definition), {"key": "other.key"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_kind_and_unit_are_locked_once_data_exists(self):
        definition = factories.MetricDefinitionFactory()
        factories.MetricSeriesFactory(
            offering_metric=factories.OfferingMetricFactory(definition=definition)
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.MetricDefinitionFactory.get_url(definition)

        for change in ({"kind": enums.MetricKinds.GAUGE}, {"unit": "h"}):
            response = self.client.patch(url, change, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.patch(url, {"name": "Completed courses"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_an_adopted_definition_cannot_be_removed(self):
        definition = factories.OfferingMetricFactory().definition
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(
            factories.MetricDefinitionFactory.get_url(definition)
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_global_definitions_are_visible_and_private_ones_are_not(self):
        factories.MetricDefinitionFactory()
        factories.MetricDefinitionFactory(
            owner_customer=structure_factories.CustomerFactory()
        )
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get(DEFINITIONS)

        self.assertEqual(len(response.data), 1)


class OfferingMetricTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer
        )
        self.definition = factories.MetricDefinitionFactory()

    def adopt(self, user, **extra):
        self.client.force_authenticate(user)
        payload = {
            "offering": self.offering.uuid.hex,
            "definition": self.definition.uuid.hex,
            **extra,
        }
        return self.client.post(OFFERING_METRICS, payload, format="json")

    def test_owner_adopts_a_definition(self):
        response = self.adopt(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["key"], self.definition.key)

    def test_service_provider_manager_adopts_lists_and_pauses(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        provider = marketplace_factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        manager = structure_factories.UserFactory()
        provider.add_user(manager, ServiceProviderRole.MANAGER)

        response = self.adopt(manager)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        uuid = response.data["uuid"]
        listed = self.client.get(OFFERING_METRICS).data
        self.assertIn(uuid, {item["uuid"] for item in listed})
        response = self.client.post(f"{OFFERING_METRICS}{uuid}/pause/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_manager_of_another_provider_cannot_adopt(self):
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        manager = structure_factories.UserFactory()
        marketplace_factories.ServiceProviderFactory().add_user(
            manager, ServiceProviderRole.MANAGER
        )

        response = self.adopt(manager)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_offering_manager_adopts_a_definition(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)

        response = self.adopt(manager)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_outsider_cannot_adopt(self):
        response = self.adopt(structure_factories.UserFactory())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_another_providers_private_definition_cannot_be_adopted(self):
        self.definition = factories.MetricDefinitionFactory(
            owner_customer=structure_factories.CustomerFactory()
        )

        response = self.adopt(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_deprecated_definition_cannot_be_adopted(self):
        self.definition.state = enums.DefinitionStates.DEPRECATED
        self.definition.save()

        response = self.adopt(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_counter_cannot_be_averaged_across_resources(self):
        response = self.adopt(
            self.fixture.owner, project_aggregation=enums.ProjectAggregations.MEAN
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lifecycle(self):
        offering_metric = factories.OfferingMetricFactory(
            offering=self.offering, definition=self.definition
        )
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingMetricFactory.get_url

        steps = (
            ("pause", status.HTTP_200_OK, enums.OfferingMetricStates.PAUSED),
            ("pause", status.HTTP_409_CONFLICT, enums.OfferingMetricStates.PAUSED),
            ("archive", status.HTTP_200_OK, enums.OfferingMetricStates.ARCHIVED),
            ("resume", status.HTTP_200_OK, enums.OfferingMetricStates.ACTIVE),
        )
        for action, code, state in steps:
            response = self.client.post(url(offering_metric, action))
            self.assertEqual(response.status_code, code, action)
            offering_metric.refresh_from_db()
            self.assertEqual(offering_metric.state, state)

    def test_a_metric_with_data_cannot_be_removed(self):
        offering_metric = factories.OfferingMetricFactory(offering=self.offering)
        factories.MetricSeriesFactory(offering_metric=offering_metric)
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.delete(
            factories.OfferingMetricFactory.get_url(offering_metric)
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_consuming_project_member_sees_the_metric_unless_archived(self):
        provider_offering = marketplace_factories.OfferingFactory()
        offering_metric = factories.OfferingMetricFactory(offering=provider_offering)
        factories.OfferingMetricFactory()
        marketplace_factories.ResourceFactory(
            offering=provider_offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        self.client.force_authenticate(self.fixture.member)

        self.assertEqual(len(self.client.get(OFFERING_METRICS).data), 1)

        offering_metric.state = enums.OfferingMetricStates.ARCHIVED
        offering_metric.save()
        self.assertEqual(len(self.client.get(OFFERING_METRICS).data), 0)


class CatalogueIntegrityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.client.force_authenticate(self.fixture.staff)

    def test_a_second_policy_with_the_same_name_is_refused(self):
        response = self.client.post(
            "/api/marketplace-metric-retention-policies/",
            {"name": "standard", "raw_days": 30, "hourly_days": 60},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_seeding_after_migrate_is_idempotent(self):
        handlers.create_default_retention_policy(sender=None)
        handlers.create_default_retention_policy(sender=None)

        self.assertEqual(
            models.RetentionPolicy.objects.filter(name="standard").count(), 1
        )

    def test_a_definition_averaged_by_an_offering_cannot_become_a_counter(self):
        definition = factories.MetricDefinitionFactory(kind=enums.MetricKinds.GAUGE)
        factories.OfferingMetricFactory(
            definition=definition,
            project_aggregation=enums.ProjectAggregations.MEAN,
        )

        response = self.client.patch(
            factories.MetricDefinitionFactory.get_url(definition),
            {"kind": enums.MetricKinds.COUNTER},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("kind", response.data)

    def test_attribute_keys_are_a_list_in_the_api(self):
        offering_metric = factories.OfferingMetricFactory()

        response = self.client.get(
            factories.OfferingMetricFactory.get_url(offering_metric)
        )

        self.assertEqual(response.data["attribute_keys"], ["course"])
