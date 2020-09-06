from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.utils import parse_date
from waldur_mastermind.invoices import tasks as invoices_tasks
from waldur_mastermind.marketplace_openstack import PACKAGE_TYPE

from .. import models, tasks
from . import factories


@freeze_time('2019-01-22')
class StatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.date = parse_date('2019-01-01')
        self.fixture = structure_fixtures.ProjectFixture()

        self.customer = self.fixture.customer
        self.project = self.fixture.project

        self.category = factories.CategoryFactory()
        self.category_component = factories.CategoryComponentFactory(
            category=self.category
        )

        self.offering = factories.OfferingFactory(
            category=self.category, type=PACKAGE_TYPE,
        )
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering, parent=self.category_component
        )

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
            start=parse_date('2019-01-01'), resource=self.resource, plan=self.plan,
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
        resource = factories.ResourceFactory(
            offering=self.offering, state=models.Resource.States.OK
        )
        self.client.force_authenticate(self.fixture.staff)
        result = self.client.get(url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]['uuid'], resource.project.customer.uuid.hex)

    def test_offering_costs_stats(self):
        with freeze_time('2020-01-01'):
            url = factories.OfferingFactory.get_url(self.offering, action='costs')

            plan = factories.PlanFactory(
                offering=self.offering, unit=UnitPriceMixin.Units.PER_DAY,
            )
            plan_component = factories.PlanComponentFactory(
                plan=plan, component=self.offering_component, amount=10
            )

            factories.ResourceFactory(
                offering=self.offering,
                state=models.Resource.States.OK,
                plan=plan,
                limits={'cpu': 1},
            )
            invoices_tasks.create_monthly_invoices()

        with freeze_time('2020-03-01'):
            self.client.force_authenticate(self.fixture.staff)
            result = self.client.get(url, {'start': '2020-01', 'end': '2020-02'})
            self.assertEqual(result.status_code, status.HTTP_200_OK)
            self.assertEqual(len(result.data), 2)
            self.assertEqual(
                result.data[0],
                {
                    'tax': 0,
                    'total': plan_component.price * 31,
                    'price': plan_component.price * 31,
                    'price_current': plan_component.price * 31,
                    'period': '2020-01',
                },
            )
