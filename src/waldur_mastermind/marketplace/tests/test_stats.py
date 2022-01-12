from ddt import data, ddt
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import parse_date, parse_datetime
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import tasks as invoices_tasks
from waldur_mastermind.marketplace import models, tasks
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace_openstack import TENANT_TYPE
from waldur_mastermind.marketplace_support import PLUGIN_NAME


class StatsBaseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project

        self.category = factories.CategoryFactory()
        self.category_component = factories.CategoryComponentFactory(
            category=self.category
        )

        self.offering = factories.OfferingFactory(
            category=self.category,
            type=TENANT_TYPE,
            state=models.Offering.States.ACTIVE,
        )
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            parent=self.category_component,
            type='cores',
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
        )


@freeze_time('2019-01-22')
class StatsTest(StatsBaseTest):
    def setUp(self):
        super(StatsTest, self).setUp()

        self.date = parse_date('2019-01-01')

        self.plan = factories.PlanFactory(offering=self.offering)
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, amount=10
        )

        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering, plan=self.plan
        )

    def test_reported_usage_is_aggregated_for_project_and_customer(self):
        # Arrange
        plan_period = models.ResourcePlanPeriod.objects.create(
            start=parse_datetime('2019-01-01'), resource=self.resource, plan=self.plan,
        )

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            date=parse_date('2019-01-10'),
            billing_period=parse_date('2019-01-01'),
            plan_period=plan_period,
            usage=100,
        )

        self.new_resource = factories.ResourceFactory(
            project=self.project, offering=self.offering, plan=self.plan
        )

        new_plan_period = models.ResourcePlanPeriod.objects.create(
            start=parse_date('2019-01-01'), resource=self.new_resource, plan=self.plan,
        )

        models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.offering_component,
            date=parse_date('2019-01-20'),
            billing_period=parse_date('2019-01-01'),
            plan_period=new_plan_period,
            usage=200,
        )

        # Act
        tasks.calculate_usage_for_current_month()

        # Assert
        project_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.project, component=self.category_component, date=self.date
            )
            .get()
            .reported_usage
        )
        customer_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.customer, component=self.category_component, date=self.date
            )
            .get()
            .reported_usage
        )

        self.assertEqual(project_usage, 300)
        self.assertEqual(customer_usage, 300)

    def test_fixed_usage_is_aggregated_for_project_and_customer(self):
        # Arrange
        models.ResourcePlanPeriod.objects.create(
            resource=self.resource,
            plan=self.plan,
            start=parse_date('2019-01-10'),
            end=parse_date('2019-01-20'),
        )

        # Act
        tasks.calculate_usage_for_current_month()

        # Assert
        project_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.project, component=self.category_component, date=self.date,
            )
            .get()
            .fixed_usage
        )
        customer_usage = (
            models.CategoryComponentUsage.objects.filter(
                scope=self.customer, component=self.category_component, date=self.date
            )
            .get()
            .fixed_usage
        )

        self.assertEqual(project_usage, self.plan_component.amount)
        self.assertEqual(customer_usage, self.plan_component.amount)

    def test_offering_customers_stats(self):
        url = factories.OfferingFactory.get_url(self.offering, action='customers')
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(
            result.data[0]['uuid'], self.resource.project.customer.uuid.hex
        )


@freeze_time('2020-01-01')
class CostsStatsTest(StatsBaseTest):
    def setUp(self):
        super(CostsStatsTest, self).setUp()
        self.url = factories.OfferingFactory.get_url(self.offering, action='costs')

        self.plan = factories.PlanFactory(
            offering=self.offering, unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, amount=10
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=models.Resource.States.OK,
            plan=self.plan,
            limits={'cores': 1},
        )
        invoices_tasks.create_monthly_invoices()

    def test_offering_costs_stats(self):
        with freeze_time('2020-03-01'):
            self._check_stats()

    def test_period_filter(self):
        self.client.force_authenticate(self.fixture.staff)

        result = self.client.get(self.url, {'other_param': ''})
        self.assertEqual(result.status_code, status.HTTP_200_OK)

        result = self.client.get(self.url, {'start': '2020-01'})
        self.assertEqual(result.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_costs_stats_if_resource_has_been_failed(self):
        with freeze_time('2020-03-01'):
            self.resource.state = models.Resource.States.ERRED
            self.resource.save()
            self._check_stats()

    def _check_stats(self):
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(self.url, {'start': '2020-01', 'end': '2020-02'})
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertDictEqual(
            result.data[0],
            {
                'tax': 0,
                'total': self.plan_component.price * 31,
                'price': self.plan_component.price * 31,
                'period': '2020-01',
            },
        )

    def test_stat_methods_are_not_available_for_anonymous_users(self):
        offering_url = factories.OfferingFactory.get_url(self.offering)

        result = self.client.get(offering_url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)

        offering_list_url = factories.OfferingFactory.get_list_url()
        result = self.client.get(offering_list_url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)

        result = self.client.get(self.url)
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)

        customers_url = factories.OfferingFactory.get_url(
            self.offering, action='customers'
        )
        result = self.client.get(customers_url)
        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)


@freeze_time('2020-03-01')
class ComponentStatsTest(StatsBaseTest):
    def setUp(self):
        super(ComponentStatsTest, self).setUp()
        self.url = factories.OfferingFactory.get_url(
            self.offering, action='component_stats'
        )

        self.plan = factories.PlanFactory(
            offering=self.offering, unit=UnitPriceMixin.Units.PER_DAY,
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, amount=10
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=models.Resource.States.OK,
            plan=self.plan,
            limits={'cores': 1},
        )

    def _create_items(self):
        invoices_tasks.create_monthly_invoices()
        invoice = invoices_models.Invoice.objects.get(
            year=2020, month=3, customer=self.resource.project.customer
        )
        return invoice.items.filter(resource_id=self.resource.id)

    def test_item_details(self):
        sp = factories.ServiceProviderFactory(customer=self.resource.offering.customer)
        component = factories.OfferingComponentFactory(
            offering=self.resource.offering,
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            type='storage',
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            billing_period=core_utils.month_start(timezone.now()),
            component=component,
        )
        item = self._create_items().first()
        self.assertDictEqual(
            item.details,
            {
                'resource_name': item.resource.name,
                'resource_uuid': item.resource.uuid.hex,
                'service_provider_name': self.resource.offering.customer.name,
                'service_provider_uuid': sp.uuid.hex,
                'offering_name': self.offering.name,
                'offering_type': TENANT_TYPE,
                'offering_uuid': self.offering.uuid.hex,
                'plan_name': self.resource.plan.name,
                'plan_uuid': self.resource.plan.uuid.hex,
                'plan_component_id': self.plan_component.id,
                'offering_component_type': self.plan_component.component.type,
                'offering_component_name': self.plan_component.component.name,
                'resource_limit_periods': [
                    {
                        'end': '2020-03-31T23:59:59.999999+00:00',
                        'start': '2020-03-01T00:00:00+00:00',
                        'total': '31',
                        'quantity': 1,
                        'billing_periods': 31,
                    }
                ],
            },
        )

    def test_component_stats_if_invoice_item_details_includes_plan_component_data(
        self,
    ):
        self.resource.offering.type = PLUGIN_NAME
        self.resource.offering.save()
        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.FIXED
        )
        self.offering_component.save()

        self._create_items()
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(self.url, {'start': '2020-03', 'end': '2020-03'})
        self.assertEqual(
            result.data,
            [
                {
                    'description': self.offering_component.description,
                    'measured_unit': self.offering_component.measured_unit,
                    'name': self.offering_component.name,
                    'period': '2020-03',
                    'date': '2020-03-31T00:00:00+00:00',
                    'type': self.offering_component.type,
                    'usage': 31,
                }
            ],
        )

    def test_handler(self):
        self.resource.offering.type = PLUGIN_NAME
        self.resource.offering.save()

        # add usage-based component to the offering and plan
        COMPONENT_TYPE = 'storage'
        new_component = factories.OfferingComponentFactory(
            offering=self.resource.offering,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
            type=COMPONENT_TYPE,
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=new_component,
        )

        self._create_items()
        plan_period = factories.ResourcePlanPeriodFactory(
            resource=self.resource,
            plan=self.plan,
            start=core_utils.month_start(timezone.now()),
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            date=timezone.now(),
            billing_period=core_utils.month_start(timezone.now()),
            component=new_component,
            plan_period=plan_period,
            usage=2,
        )
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(self.url, {'start': '2020-03', 'end': '2020-03'})
        component_cores = self.resource.offering.components.get(type='cores')
        component_storage = self.resource.offering.components.get(type='storage')
        self.assertEqual(len(result.data), 2)
        self.assertEqual(
            [r for r in result.data if r['type'] == component_cores.type][0],
            {
                'description': component_cores.description,
                'measured_unit': component_cores.measured_unit,
                'name': component_cores.name,
                'period': '2020-03',
                'date': '2020-03-31T00:00:00+00:00',
                'type': component_cores.type,
                'usage': 31,  # days in March of 1 core usage with per-day plan
            },
        )
        self.assertEqual(
            [r for r in result.data if r['type'] == component_storage.type][0],
            {
                'description': component_storage.description,
                'measured_unit': component_storage.measured_unit,
                'name': component_storage.name,
                'period': '2020-03',
                'date': '2020-03-31T00:00:00+00:00',
                'type': component_storage.type,
                'usage': 2,
            },
        )


@ddt
class CustomerStatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data(
        'staff', 'global_support',
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get('/api/marketplace-stats/project_member_count/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get('/api/marketplace-stats/project_member_count/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class LimitsStatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource_1 = factories.ResourceFactory(
            limits={'cpu': 5}, state=models.Resource.States.OK
        )
        factories.ResourceFactory(
            limits={'cpu': 2},
            state=models.Resource.States.OK,
            offering=self.resource_1.offering,
        )
        self.resource_2 = factories.ResourceFactory(
            limits={'cpu': 10, 'ram': 1}, state=models.Resource.States.OK
        )
        self.url = '/api/marketplace-stats/resources_limits/'

        self.division_1 = structure_factories.DivisionFactory()
        self.division_2 = structure_factories.DivisionFactory()
        self.resource_1.offering.divisions.add(self.division_1, self.division_2)

    @data(
        'staff', 'global_support',
    )
    def test_user_can_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            [
                {
                    'offering_uuid': self.resource_1.offering.uuid,
                    'name': 'cpu',
                    'value': 7,
                    'division_name': self.division_1.name,
                    'division_uuid': self.division_1.uuid.hex,
                },
                {
                    'offering_uuid': self.resource_1.offering.uuid,
                    'name': 'cpu',
                    'value': 7,
                    'division_name': self.division_2.name,
                    'division_uuid': self.division_2.uuid.hex,
                },
                {
                    'offering_uuid': self.resource_2.offering.uuid,
                    'name': 'cpu',
                    'value': 10,
                    'division_name': '',
                    'division_uuid': '',
                },
                {
                    'offering_uuid': self.resource_2.offering.uuid,
                    'name': 'ram',
                    'value': 1,
                    'division_name': '',
                    'division_uuid': '',
                },
            ],
        )

    @data('owner', 'user', 'customer_support', 'admin', 'manager')
    def test_user_cannot_get_marketplace_stats(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
