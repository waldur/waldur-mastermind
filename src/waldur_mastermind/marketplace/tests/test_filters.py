from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, plugins
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import utils as test_utils


class CustomerResourcesFilterTest(test.APITransactionTestCase):
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
            return self.client.get(list_url, {'has_resources': has_resources}).data
        else:
            return self.client.get(list_url).data

    def test_list_customers_with_resources(self):
        self.assertEqual(1, len(self.list_customers(True)))

    def test_list_all_customers(self):
        self.assertEqual(2, len(self.list_customers(False)))


class ServiceProviderFilterTest(test.APITransactionTestCase):
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
            list_url, {'service_provider_uuid': service_provider_uuid}
        ).data

    def test_list_offering_customers(self):
        customers = self.list_customers(self.service_provider1.uuid.hex)
        self.assertEqual(1, len(customers))
        self.assertEqual(customers[0]['uuid'], self.resource1.project.customer.uuid.hex)

    def test_list_is_empty_if_offering_does_not_have_customers(self):
        self.assertEqual(0, len(self.list_customers(self.service_provider2.uuid.hex)))

    def test_filter_customer_keyword(self):
        list_url = factories.ServiceProviderFactory.get_list_url()
        provider_1 = factories.ServiceProviderFactory()
        factories.ServiceProviderFactory()
        provider_1.customer.name = 'It is test_name.'
        provider_1.customer.abbreviation = 'test abbr'
        provider_1.customer.save()
        self.client.force_authenticate(self.fixture1.staff)

        response = self.client.get(list_url, {'customer_keyword': 'test_name'})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(response.data[0]['uuid'], provider_1.uuid.hex)

        response = self.client.get(list_url, {'customer_keyword': 'abbr'})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(response.data[0]['uuid'], provider_1.uuid.hex)


class ResourceFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.UserFixture()
        self.resource_1 = factories.ResourceFactory(
            backend_metadata={
                'external_ips': ['200.200.200.200', '200.200.200.201'],
                'internal_ips': ['192.168.42.1', '192.168.42.2'],
            },
            backend_id='backend_id',
        )
        factories.ResourceFactory(backend_id='other_backend_id')

        self.url = factories.ResourceFactory.get_list_url()

    def test_backend_id_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {'backend_id': 'backend_id'})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.resource_1.uuid.hex)

    def test_backend_metadata_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        # check external IP lookup
        response = self.client.get(self.url, {'query': '200.200.200.200'})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.resource_1.uuid.hex)

        # check internal IP lookup
        response = self.client.get(self.url, {'query': '192.168.42.1'})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.resource_1.uuid.hex)

    def test_field_filter(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {'field': ['state', 'offering']})
        self.assertTrue(all([len(fields) == 2 for fields in response.data]))


class FilterByScopeUUIDTest(test.APITransactionTestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type='TEST_TYPE',
            create_resource_processor=test_utils.TestCreateProcessor,
        )
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.type = 'TEST_TYPE'
        self.fixture.offering.save()
        self.url = factories.ResourceFactory.get_list_url()
        self.scope = structure_factories.TestNewInstanceFactory()

    def test_scope_uuid_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {'query': self.scope.uuid.hex})
        self.assertEqual(len(response.data), 0)

        self.fixture.resource.scope = self.scope
        self.fixture.resource.save()
        response = self.client.get(self.url, {'query': self.scope.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.fixture.resource.uuid.hex)


class OrderFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.url = factories.OrderFactory.get_list_url()

    def test_order_items_type_filter_positive(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {'type': 'Create'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_order_items_type_filter_negative(self):
        self.fixture.order_item.type = models.RequestTypeMixin.Types.UPDATE
        self.fixture.order_item.save()
        user = self.fixture.staff
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {'type': 'Create'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


class CategoryFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.state = models.Offering.States.ACTIVE
        self.offering.save()
        self.category = self.offering.category
        self.customer = self.offering.customer
        self.url = factories.CategoryFactory.get_list_url()
        factories.CategoryFactory()

    def test_customer_uuid_filter_positive(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {'customer_uuid': self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]['uuid'], self.category.uuid.hex)
        self.assertEqual(response.data[0]['offering_count'], 1)

    def test_customer_uuid_filter_negative(self):
        new_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {'customer_uuid': new_customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_customer_uuid_filter_with_offering_state_positive(self):
        self.client.force_authenticate(self.fixture.staff)
        self.offering.state = 1
        self.offering.save()
        response = self.client.get(
            self.url,
            {'customer_uuid': self.customer.uuid.hex, 'customers_offerings_state': 1},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.data[0]['uuid'], self.category.uuid.hex)
        self.assertEqual(response.data[0]['offering_count'], 1)

    def test_customer_uuid_filter_with_offering_state_negative(self):
        new_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.staff)
        self.offering.state = 2
        self.offering.save()
        response = self.client.get(
            self.url,
            {'customer_uuid': new_customer.uuid.hex, 'customers_offerings_state': 1},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


class PlanComponentFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture_1 = fixtures.MarketplaceFixture()
        self.fixture_2 = fixtures.MarketplaceFixture()
        self.fixture_1.offering.shared = True
        self.fixture_1.offering.state = models.Offering.States.ACTIVE
        self.fixture_1.offering.save()
        self.fixture_2.offering.shared = True
        self.fixture_2.offering.state = models.Offering.States.ACTIVE
        self.fixture_2.offering.save()
        self.url = factories.PlanComponentFactory.get_list_url()

    def test_offering_uuid_filter(self):
        self.client.force_authenticate(self.fixture_1.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), 2)
        response = self.client.get(
            self.url, {'offering_uuid': self.fixture_1.offering.uuid.hex},
        )
        self.assertEqual(len(response.json()), 1)
