import json
import unittest

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import utils as test_utils
from waldur_mastermind.proposal.enums import CallStates, RequestedOfferingStates
from waldur_mastermind.proposal.tests import factories as proposal_factories


class CustomerResourcesFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture1 = structure_fixtures.ServiceFixture()
        self.customer1 = self.fixture1.customer
        self.offering = factories.OfferingFactory(customer=self.customer1)
        self.resource1 = factories.ResourceFactory(
            offering=self.offering, project=self.fixture1.project
        )

        self.fixture2 = structure_fixtures.ServiceFixture()
        self.customer2 = self.fixture2.customer

    def list_customers(self, has_resources):
        list_url = structure_factories.CustomerFactory.get_list_url()
        self.client.force_authenticate(self.fixture1.staff)
        if has_resources:
            return self.client.get(list_url, {"has_resources": has_resources}).data
        else:
            return self.client.get(list_url).data

    def test_list_customers_with_resources(self):
        self.assertEqual(1, len(self.list_customers(True)))

    def test_list_all_customers(self):
        self.assertEqual(2, len(self.list_customers(False)))


class ServiceProviderFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture1 = structure_fixtures.ServiceFixture()
        self.service_provider1 = self.fixture1.customer
        self.offering1 = factories.OfferingFactory(customer=self.service_provider1)
        self.resource1 = factories.ResourceFactory(
            offering=self.offering1, project=self.fixture1.project
        )

        self.fixture2 = structure_fixtures.ServiceFixture()
        self.service_provider2 = self.fixture2.customer
        factories.OfferingFactory(customer=self.service_provider2)

    def list_customers(self, service_provider_uuid):
        list_url = structure_factories.CustomerFactory.get_list_url()
        self.client.force_authenticate(self.fixture1.staff)
        return self.client.get(
            list_url, {"service_provider_uuid": service_provider_uuid}
        ).data

    def test_list_offering_customers(self):
        customers = self.list_customers(self.service_provider1.uuid.hex)
        self.assertEqual(1, len(customers))
        self.assertEqual(customers[0]["uuid"], self.resource1.project.customer.uuid.hex)

    def test_list_is_empty_if_offering_does_not_have_customers(self):
        self.assertEqual(0, len(self.list_customers(self.service_provider2.uuid.hex)))

    def test_filter_customer_keyword(self):
        list_url = factories.ServiceProviderFactory.get_list_url()
        provider_1 = factories.ServiceProviderFactory()
        factories.ServiceProviderFactory()
        provider_1.customer.name = "It is test_name."
        provider_1.customer.abbreviation = "test abbr"
        provider_1.customer.save()
        self.client.force_authenticate(self.fixture1.staff)

        response = self.client.get(list_url, {"customer_keyword": "test_name"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(response.data[0]["uuid"], provider_1.uuid.hex)

        response = self.client.get(list_url, {"customer_keyword": "abbr"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(response.data[0]["uuid"], provider_1.uuid.hex)


class ResourceFilterTest(test.APITestCase):
    def setUp(self):
        with freeze_time("2020-01-01"):
            self.fixture = fixtures.MarketplaceFixture()
            self.resource_1 = factories.ResourceFactory(
                backend_metadata={
                    "external_ips": ["200.200.200.200", "200.200.200.201"],
                    "internal_ips": ["192.168.42.1", "192.168.42.2"],
                },
                backend_id="backend_id",
            )

        with freeze_time("2021-01-01"):
            factories.ResourceFactory(backend_id="other_backend_id")

        self.url = factories.ResourceFactory.get_list_url()

    def test_backend_id_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"backend_id": "backend_id"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource_1.uuid.hex)

    def test_backend_metadata_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        # check external IP lookup
        response = self.client.get(self.url, {"query": "200.200.200.200"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource_1.uuid.hex)

        # check internal IP lookup
        response = self.client.get(self.url, {"query": "192.168.42.1"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource_1.uuid.hex)

    def test_flavor_name_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        matching = factories.ResourceFactory(
            backend_metadata={"flavor_name": "m1.large"}
        )
        other = factories.ResourceFactory(backend_metadata={"flavor_name": "m1.small"})
        response = self.client.get(self.url, {"flavor_name": "large"})
        uuids = [r["uuid"] for r in response.data]
        self.assertIn(matching.uuid.hex, uuids)
        self.assertNotIn(other.uuid.hex, uuids)

    def test_image_name_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        matching = factories.ResourceFactory(
            backend_metadata={"image_name": "Ubuntu 22.04"}
        )
        other = factories.ResourceFactory(backend_metadata={"image_name": "CentOS 9"})
        response = self.client.get(self.url, {"image_name": "ubuntu"})
        uuids = [r["uuid"] for r in response.data]
        self.assertIn(matching.uuid.hex, uuids)
        self.assertNotIn(other.uuid.hex, uuids)

    def test_field_filter(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {"field": ["state", "offering"]})
        self.assertTrue(all([len(fields) == 2 for fields in response.data]))

    def test_filter_created(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 3)
        response = self.client.get(self.url, {"created": "2021-01-01"})
        self.assertEqual(len(response.data), 1)

    def test_filter_visible_to_username(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 3)
        response = self.client.get(
            self.url, {"visible_to_username": self.fixture.admin.username}
        )
        self.assertEqual(len(response.data), 1)

    def test_filter_visible_to_providers(self):
        self.client.force_authenticate(self.fixture.staff)

        # Case 1: Resource is Creating, Order is Pending Provider. Should be visible.
        resource1 = factories.ResourceFactory(state=ResourceStates.CREATING)
        factories.OrderFactory(
            resource=resource1,
            state=OrderStates.PENDING_PROVIDER,
            project=resource1.project,
        )

        # Case 2: Resource is Creating, Order is Pending Consumer. Should be hidden.
        resource2 = factories.ResourceFactory(state=ResourceStates.CREATING)
        factories.OrderFactory(
            resource=resource2,
            state=OrderStates.PENDING_CONSUMER,
            project=resource2.project,
        )

        # Case 3: Resource is Creating, Order is Pending Project. Should be hidden.
        resource3 = factories.ResourceFactory(state=ResourceStates.CREATING)
        factories.OrderFactory(
            resource=resource3,
            state=OrderStates.PENDING_PROJECT,
            project=resource3.project,
        )

        # Case 4: Resource is Creating, Order is Executing. Should be visible.
        resource4 = factories.ResourceFactory(state=ResourceStates.CREATING)
        factories.OrderFactory(
            resource=resource4,
            state=OrderStates.EXECUTING,
            project=resource4.project,
        )

        response = self.client.get(self.url, {"visible_to_providers": "true"})

        uuids = [r["uuid"] for r in response.data]
        self.assertIn(resource1.uuid.hex, uuids)
        self.assertNotIn(resource2.uuid.hex, uuids)
        self.assertNotIn(resource3.uuid.hex, uuids)
        self.assertIn(resource4.uuid.hex, uuids)

    def test_is_attached_filter(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create attached resource
        attached_resource = factories.ResourceFactory(
            backend_metadata={"instance_name": "VM-1"}
        )

        # Create unattached resource
        unattached_resource = factories.ResourceFactory(
            backend_metadata={"volume_size": 100}
        )

        # Filter by is_attached=true
        response = self.client.get(self.url, {"is_attached": "true"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], attached_resource.uuid.hex)

        # Filter by is_attached=false
        # Note: self.resource_1 and fixture resources are also unattached
        response = self.client.get(self.url, {"is_attached": "false"})

        uuids = [r["uuid"] for r in response.data]
        self.assertIn(unattached_resource.uuid.hex, uuids)
        self.assertNotIn(attached_resource.uuid.hex, uuids)

    def test_resource_attributes_exact_match(self):
        self.client.force_authenticate(self.fixture.staff)
        resource = factories.ResourceFactory(
            attributes={"storage_data_type": "store", "tier": "hot"},
        )
        response = self.client.get(
            self.url,
            {"resource_attributes": json.dumps({"storage_data_type": "store"})},
        )
        uuids = [r["uuid"] for r in response.data]
        self.assertIn(resource.uuid.hex, uuids)

    def test_resource_attributes_no_match(self):
        self.client.force_authenticate(self.fixture.staff)
        factories.ResourceFactory(
            attributes={"storage_data_type": "store"},
        )
        response = self.client.get(
            self.url,
            {"resource_attributes": json.dumps({"storage_data_type": "archive"})},
        )
        uuids = [r["uuid"] for r in response.data]
        self.assertEqual(len(uuids), 0)

    def test_resource_attributes_invalid_json(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {"resource_attributes": "not-valid-json"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class FilterByScopeUUIDTest(test.APITestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=test_utils.TestCreateProcessor,
        )
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.type = "TEST_TYPE"
        self.fixture.offering.save()
        self.url = factories.ResourceFactory.get_list_url()
        self.scope = structure_factories.TestNewInstanceFactory()

    def test_scope_uuid_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": self.scope.uuid.hex})
        self.assertEqual(len(response.data), 0)

        self.fixture.resource.scope = self.scope
        self.fixture.resource.save()
        response = self.client.get(self.url, {"query": self.scope.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.resource.uuid.hex)


class OrderFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.OrderFactory.get_list_url()

    def test_type_filter_positive(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"type": "Create"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_type_filter_negative(self):
        self.fixture.order.type = OrderTypes.UPDATE
        self.fixture.order.save()
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"type": "Create"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


class CategoryFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        self.category = self.offering.category
        self.customer = self.offering.customer
        self.url = factories.CategoryFactory.get_list_url()
        factories.CategoryFactory()

    def test_customer_uuid_filter_positive(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"customer_uuid": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]["uuid"], self.category.uuid.hex)
        self.assertEqual(response.data[0]["offering_count"], 1)

    def test_customer_uuid_filter_negative(self):
        new_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"customer_uuid": new_customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    @unittest.skip("Temporary disable till counters are fixed")
    def test_customer_uuid_filter_with_offering_state_positive(self):
        self.client.force_authenticate(self.fixture.staff)
        self.offering.state = 1
        self.offering.save()
        response = self.client.get(
            self.url,
            {"customer_uuid": self.customer.uuid.hex, "customers_offerings_state": 1},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]["uuid"], self.category.uuid.hex)
        self.assertEqual(response.data[0]["offering_count"], 1)

    def test_customer_uuid_filter_with_offering_state_negative(self):
        new_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.staff)
        self.offering.state = 2
        self.offering.save()
        response = self.client.get(
            self.url,
            {"customer_uuid": new_customer.uuid.hex, "customers_offerings_state": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    @unittest.skip("Temporary disable till counters are fixed")
    def test_offering_count_if_shared_is_passed(self):
        factories.OfferingFactory(
            category=self.category,
            customer=self.customer,
            state=OfferingStates.ACTIVE,
            shared=False,
        )
        url = factories.CategoryFactory.get_url(self.category)

        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offering_count"], 2)

        response = self.client.get(url, {"shared": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offering_count"], 1)

        response = self.client.get(url, {"shared": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offering_count"], 1)

    def test_category_has_shared(self):
        self.offering.shared = False
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"has_shared": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        self.offering.shared = True
        self.offering.save()

        response = self.client.get(self.url, {"has_shared": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


class CategoryOrderingTest(test.APITestCase):
    def setUp(self):
        self.url = factories.CategoryFactory.get_list_url()
        self.staff = structure_factories.UserFactory(is_staff=True)
        models.Category.objects.all().delete()

        self.group_a = factories.CategoryGroupFactory(title="Alpha")
        self.group_b = factories.CategoryGroupFactory(title="Beta")
        self.cat_b_storage = factories.CategoryFactory(
            title="Storage", group=self.group_b
        )
        self.cat_a_storage = factories.CategoryFactory(
            title="Storage", group=self.group_a
        )
        self.cat_a_compute = factories.CategoryFactory(
            title="Compute", group=self.group_a
        )

    def _uuids(self, response):
        return [row["uuid"] for row in response.data if row["group"]]

    def test_default_ordering_is_by_group_then_title(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._uuids(response),
            [
                self.cat_a_compute.uuid.hex,
                self.cat_a_storage.uuid.hex,
                self.cat_b_storage.uuid.hex,
            ],
        )

    def test_ordering_by_title_desc(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"o": "-title"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [row["title"] for row in response.data]
        self.assertEqual(titles, sorted(titles, reverse=True))


class PlanComponentFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture_1 = fixtures.MarketplaceFixture()
        self.fixture_2 = fixtures.MarketplaceFixture()
        self.fixture_1.offering.shared = True
        self.fixture_1.offering.state = OfferingStates.ACTIVE
        self.fixture_1.offering.save()
        self.fixture_2.offering.shared = True
        self.fixture_2.offering.state = OfferingStates.ACTIVE
        self.fixture_2.offering.save()
        self.url = factories.PlanComponentFactory.get_list_url()

    def test_offering_uuid_filter(self):
        self.client.force_authenticate(self.fixture_1.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)
        response = self.client.get(
            self.url,
            {"offering_uuid": self.fixture_1.offering.uuid.hex},
        )
        self.assertEqual(len(response.json()), 1)


class AccessibleViaCallsFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_public_list_url()

    def test_accessible_via_calls(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"accessible_via_calls": "true"})
        self.assertEqual(len(response.json()), 0)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"accessible_via_calls": "false"})
        self.assertEqual(len(response.json()), 1)

        requested_offering = proposal_factories.RequestedOfferingFactory(
            offering=self.offering,
            state=RequestedOfferingStates.ACCEPTED,
        )
        requested_offering.call.state = CallStates.ACTIVE
        requested_offering.call.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"accessible_via_calls": "true"})
        self.assertEqual(len(response.json()), 1)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"accessible_via_calls": "false"})
        self.assertEqual(len(response.json()), 0)


class ResourceBillingTypeFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.ResourceFactory.get_list_url()

        # Create offering with usage-based components
        self.usage_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.usage_offering, billing_type="usage"
        )
        self.usage_resource = factories.ResourceFactory(
            offering=self.usage_offering, project=self.fixture.project
        )

        # Create offering with limit-based components
        self.limit_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.limit_offering, billing_type="limit"
        )
        self.limit_resource = factories.ResourceFactory(
            offering=self.limit_offering, project=self.fixture.project
        )

        # Create offering with fixed components
        self.fixed_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.fixed_offering, billing_type="fixed"
        )
        self.fixed_resource = factories.ResourceFactory(
            offering=self.fixed_offering, project=self.fixture.project
        )

    def test_usage_based_filter_true(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"usage_based": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertIn(self.usage_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.limit_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.fixed_resource.uuid.hex, resource_uuids)

    def test_usage_based_filter_false(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"usage_based": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.usage_resource.uuid.hex, resource_uuids)

    def test_limit_based_filter_true(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"limit_based": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertIn(self.limit_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.usage_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.fixed_resource.uuid.hex, resource_uuids)

    def test_limit_based_filter_false(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"limit_based": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.limit_resource.uuid.hex, resource_uuids)

    def test_combined_filters(self):
        # Test that we can combine both filters
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"usage_based": "true", "limit_based": "true"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return empty result since no resource can be both usage-based and limit-based
        self.assertEqual(len(response.data), 0)

    def test_no_filter_returns_all(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return all resources including the fixture resource
        self.assertGreaterEqual(len(response.data), 4)

    def test_only_limit_based_filter_true(self):
        # Test filter that includes only resources with only limit-based components
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"only_limit_based": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        # Should include only limit-only resources
        self.assertIn(self.limit_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.usage_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.fixed_resource.uuid.hex, resource_uuids)

    def test_only_limit_based_filter_false(self):
        # Test filter that excludes resources with only limit-based components
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"only_limit_based": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        # Should exclude limit-only resources but include mixed and non-limit resources
        self.assertNotIn(self.limit_resource.uuid.hex, resource_uuids)
        self.assertIn(self.usage_resource.uuid.hex, resource_uuids)
        self.assertIn(self.fixed_resource.uuid.hex, resource_uuids)

    def test_only_usage_based_filter_true(self):
        # Test filter that includes only resources with only usage-based components
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"only_usage_based": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        # Should include only usage-only resources
        self.assertIn(self.usage_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.limit_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.fixed_resource.uuid.hex, resource_uuids)

    def test_only_usage_based_filter_false(self):
        # Test filter that excludes resources with only usage-based components
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"only_usage_based": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        # Should exclude usage-only resources but include mixed and non-usage resources
        self.assertNotIn(self.usage_resource.uuid.hex, resource_uuids)
        self.assertIn(self.limit_resource.uuid.hex, resource_uuids)
        self.assertIn(self.fixed_resource.uuid.hex, resource_uuids)


class ComponentCountFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.ResourceFactory.get_list_url()

        # Create offering with 1 component (limit-based)
        self.single_component_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.single_component_offering, billing_type="limit", type="cpu"
        )
        self.single_component_resource = factories.ResourceFactory(
            offering=self.single_component_offering, project=self.fixture.project
        )

        # Create offering with 2 components (1 limit, 1 usage)
        self.two_component_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.two_component_offering, billing_type="limit", type="cpu"
        )
        factories.OfferingComponentFactory(
            offering=self.two_component_offering, billing_type="usage", type="ram"
        )
        self.two_component_resource = factories.ResourceFactory(
            offering=self.two_component_offering, project=self.fixture.project
        )

        # Create offering with 3 components (2 limit, 1 fixed)
        self.three_component_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.three_component_offering, billing_type="limit", type="cpu"
        )
        factories.OfferingComponentFactory(
            offering=self.three_component_offering, billing_type="limit", type="ram"
        )
        factories.OfferingComponentFactory(
            offering=self.three_component_offering, billing_type="fixed", type="storage"
        )
        self.three_component_resource = factories.ResourceFactory(
            offering=self.three_component_offering, project=self.fixture.project
        )

        # Create offering with no components
        self.no_component_offering = factories.OfferingFactory()
        self.no_component_resource = factories.ResourceFactory(
            offering=self.no_component_offering, project=self.fixture.project
        )

    def test_component_count_filter_one(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"component_count": "1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_component_count_filter_two(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"component_count": "2"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_component_count_filter_three(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"component_count": "3"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_component_count_filter_zero(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"component_count": "0"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_limit_component_count_filter_one(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"limit_component_count": "1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_limit_component_count_filter_two(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"limit_component_count": "2"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_limit_component_count_filter_zero(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"limit_component_count": "0"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_combined_component_filters(self):
        # Test filtering for resources with exactly 2 total components and 1 limit component
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"component_count": "2", "limit_component_count": "1"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertNotIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_combined_with_existing_filters(self):
        # Test combining new filters with existing limit_based filter
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"component_count": "1", "limit_based": "true"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        self.assertIn(self.single_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.two_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.three_component_resource.uuid.hex, resource_uuids)
        self.assertNotIn(self.no_component_resource.uuid.hex, resource_uuids)

    def test_invalid_component_count_returns_empty(self):
        # Test with non-existent component count
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"component_count": "999"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_no_filter_returns_all(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return all resources including the fixture resource
        self.assertGreaterEqual(len(response.data), 5)


class OnlyUsageBasedFilterRealWorldTest(test.APITestCase):
    """Test the only_usage_based filter with real-world scenario to ensure the fix works"""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.ResourceFactory.get_list_url()

        # Create offering with ONLY limit-based components (like the user reported)
        self.limit_only_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.limit_only_offering, billing_type="limit", type="cpu"
        )
        self.limit_only_resource = factories.ResourceFactory(
            offering=self.limit_only_offering, project=self.fixture.project
        )

        # Create offering with ONLY usage-based components
        self.usage_only_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=self.usage_only_offering, billing_type="usage", type="ram"
        )
        self.usage_only_resource = factories.ResourceFactory(
            offering=self.usage_only_offering, project=self.fixture.project
        )

    def test_only_usage_based_true_includes_usage_only_resources(self):
        """Test that only_usage_based=true includes only resources with only usage-based components"""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"only_usage_based": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        # Should include only usage-only resources
        self.assertIn(self.usage_only_resource.uuid.hex, resource_uuids)
        # Should exclude limit-only resources (they are not usage-only)
        self.assertNotIn(self.limit_only_resource.uuid.hex, resource_uuids)

    def test_only_usage_based_false_excludes_usage_only_resources(self):
        """Test that only_usage_based=false excludes resources with only usage-based components"""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"only_usage_based": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resource_uuids = [r["uuid"] for r in response.data]
        # Should exclude usage-only resources
        self.assertNotIn(self.usage_only_resource.uuid.hex, resource_uuids)
        # Should include limit-only resources
        self.assertIn(self.limit_only_resource.uuid.hex, resource_uuids)


class ComponentUsageFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = factories.ComponentUsageFactory.get_list_url()

        # Create component usage for 2019-06
        self.usage_2019_06 = factories.ComponentUsageFactory(
            billing_period=core_utils.month_start(parse_datetime("2019-06-15")),
            date=parse_datetime("2019-06-15"),
        )

        # Create component usage for 2019-12
        self.usage_2019_12 = factories.ComponentUsageFactory(
            billing_period=core_utils.month_start(parse_datetime("2019-12-15")),
            date=parse_datetime("2019-12-15"),
        )

        # Create component usage for 2020-06
        self.usage_2020_06 = factories.ComponentUsageFactory(
            billing_period=core_utils.month_start(parse_datetime("2020-06-15")),
            date=parse_datetime("2020-06-15"),
        )

    def test_billing_period_year_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"billing_period_year": "2019"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        usage_uuids = [u["uuid"] for u in response.data]
        self.assertIn(self.usage_2019_06.uuid.hex, usage_uuids)
        self.assertIn(self.usage_2019_12.uuid.hex, usage_uuids)
        self.assertNotIn(self.usage_2020_06.uuid.hex, usage_uuids)

    def test_billing_period_month_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"billing_period_month": "6"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        usage_uuids = [u["uuid"] for u in response.data]
        self.assertIn(self.usage_2019_06.uuid.hex, usage_uuids)
        self.assertNotIn(self.usage_2019_12.uuid.hex, usage_uuids)
        self.assertIn(self.usage_2020_06.uuid.hex, usage_uuids)

    def test_combined_billing_period_filters(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"billing_period_year": "2019", "billing_period_month": "6"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        usage_uuids = [u["uuid"] for u in response.data]
        self.assertIn(self.usage_2019_06.uuid.hex, usage_uuids)
        self.assertNotIn(self.usage_2019_12.uuid.hex, usage_uuids)
        self.assertNotIn(self.usage_2020_06.uuid.hex, usage_uuids)

    def test_no_billing_period_filter_returns_all(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return all component usages
        self.assertGreaterEqual(len(response.data), 3)


class OfferingQueryFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering1 = factories.OfferingFactory(
            name="Alpha Cloud Service", description="Premium cloud hosting"
        )
        self.offering2 = factories.OfferingFactory(
            name="Beta Analytics", description="Data processing service"
        )
        self.offering3 = factories.OfferingFactory(
            name="Gamma Storage", description="Reliable data storage"
        )
        # Manually set specific slugs for predictable testing
        self.offering1.slug = "alpha-cloud-service"
        self.offering1.save()
        self.offering2.slug = "beta-analytics"
        self.offering2.save()
        self.offering3.slug = "gamma-storage"
        self.offering3.save()

        self.url = factories.OfferingFactory.get_list_url()

    def test_query_filter_by_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "Alpha"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering1.uuid.hex)

    def test_query_filter_by_slug(self):
        self.client.force_authenticate(self.fixture.staff)

        # Test exact slug match
        response = self.client.get(self.url, {"query": "beta-analytics"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering2.uuid.hex)

        # Test partial slug match
        response = self.client.get(self.url, {"query": "gamma"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering3.uuid.hex)

    def test_query_filter_by_description(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "Premium"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering1.uuid.hex)

    def test_query_filter_by_uuid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": self.offering1.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering1.uuid.hex)


class OrderQueryFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.order1 = factories.OrderFactory(project=self.fixture.project)
        self.order2 = factories.OrderFactory(project=self.fixture.project)
        # Manually set specific slugs for predictable testing
        self.order1.slug = "order-alpha-123"
        self.order1.save()
        self.order2.slug = "order-beta-456"
        self.order2.save()
        # Also create a resource with a specific name
        self.order1.attributes = {"name": "Test Resource Alpha"}
        self.order1.save()

        self.url = factories.OrderFactory.get_list_url()

    def test_query_filter_by_slug(self):
        self.client.force_authenticate(self.fixture.staff)

        # Test exact slug match
        response = self.client.get(self.url, {"query": "order-alpha-123"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.order1.uuid.hex)

        # Test partial slug match
        response = self.client.get(self.url, {"query": "beta"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.order2.uuid.hex)

    def test_query_filter_by_project_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": self.fixture.project.name})
        # Should return all orders from this project
        self.assertGreaterEqual(len(response.data), 2)

    def test_query_filter_by_resource_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "Test Resource Alpha"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.order1.uuid.hex)

    def test_query_filter_by_uuid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": self.order1.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.order1.uuid.hex)


class ResourceQueryFilterSlugTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource1 = factories.ResourceFactory(
            name="Alpha Database",
            backend_id="alpha-db-001",
            project=self.fixture.project,
        )
        self.resource2 = factories.ResourceFactory(
            name="Beta Cache", backend_id="beta-cache-002", project=self.fixture.project
        )
        # Manually set specific slugs for predictable testing
        self.resource1.slug = "alpha-database"
        self.resource1.save()
        self.resource2.slug = "beta-cache"
        self.resource2.save()

        self.url = factories.ResourceFactory.get_list_url()

    def test_query_filter_by_slug(self):
        self.client.force_authenticate(self.fixture.staff)

        # Test exact slug match
        response = self.client.get(self.url, {"query": "alpha-database"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource1.uuid.hex)

        # Test partial slug match
        response = self.client.get(self.url, {"query": "beta"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource2.uuid.hex)

    def test_query_filter_by_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "Alpha Database"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource1.uuid.hex)

    def test_query_filter_by_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": "alpha-db-001"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource1.uuid.hex)

    def test_query_filter_by_uuid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"query": self.resource1.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.resource1.uuid.hex)
