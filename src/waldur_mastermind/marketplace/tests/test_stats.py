from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.utils import parse_date

from .. import models, tasks
from . import factories


@freeze_time('2019-01-22')
class StatsTest(test.APITransactionTestCase):
    def setUp(self):
        self.date = parse_date('2019-01-01')
        fixture = structure_fixtures.ProjectFixture()

        self.customer = fixture.customer
        self.project = fixture.project

        self.category = factories.CategoryFactory()
        self.category_component = factories.CategoryComponentFactory(
            category=self.category
        )

        self.offering = factories.OfferingFactory(category=self.category)
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
