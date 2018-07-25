import datetime

from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from freezegun import freeze_time

from waldur_core.cost_tracking import models, CostTrackingRegister, tasks
from waldur_core.cost_tracking.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.models import TestNewInstance


class RecalculateEstimateTest(TransactionTestCase):

    def setUp(self):
        resource_content_type = ContentType.objects.get_for_model(TestNewInstance)
        self.price_list_item = models.DefaultPriceListItem.objects.create(
            item_type='storage', key='1 MB', resource_content_type=resource_content_type, value=2)
        CostTrackingRegister.register_strategy(factories.TestNewInstanceCostTrackingStrategy)
        self.start_time = datetime.datetime(2016, 8, 8, 11, 0)
        with freeze_time(self.start_time):
            self.resource = structure_factories.TestNewInstanceFactory(disk=20 * 1024)
        self.spl = self.resource.service_project_link
        self.project = self.spl.project
        self.customer = self.project.customer
        self.service = self.spl.service

    def test_consumed_is_recalculated_properly_for_resource(self):
        calculation_time = datetime.datetime(2016, 8, 8, 15, 0)
        with freeze_time(calculation_time):
            tasks.recalculate_estimate()
            price_estimate = models.PriceEstimate.objects.get_current(scope=self.resource)

        working_minutes = (calculation_time - self.start_time).total_seconds() / 60
        expected = working_minutes * self.price_list_item.minute_rate * self.resource.disk
        self.assertAlmostEqual(price_estimate.consumed, expected)

    def test_consumed_is_recalculated_properly_for_ancestors(self):
        with freeze_time(self.start_time):
            self.second_resource = structure_factories.TestNewInstanceFactory(
                disk=10 * 1024, service_project_link=self.spl)

        calculation_time = datetime.datetime(2016, 8, 8, 15, 0)
        with freeze_time(calculation_time):
            tasks.recalculate_estimate()
            price_estimates = [models.PriceEstimate.objects.get_current(scope=ancestor) for ancestor in
                               (self.customer, self.service, self.spl, self.project)]

        working_minutes = (calculation_time - self.start_time).total_seconds() / 60
        # each ancestor is connected with 2 resources
        expected = working_minutes * self.price_list_item.minute_rate * (self.resource.disk + self.second_resource.disk)
        for price_estimate in price_estimates:
            message = 'Price estimate "consumed" is calculated wrongly for "%s". Real value: %s, expected: %s.' % (
                price_estimate.scope, price_estimate.consumed, expected)
            self.assertAlmostEqual(price_estimate.consumed, expected, msg=message)

    def test_new_estimates_are_created_in_new_month(self):
        month_start = datetime.datetime(2016, 9, 1, 0, 0)
        month_end = datetime.datetime(2016, 9, 30, 23, 59, 59)
        calculation_time = datetime.datetime(2016, 9, 1, 1, 0)
        with freeze_time(calculation_time):
            tasks.recalculate_estimate()
            price_estimates = [models.PriceEstimate.objects.get_current(scope=scope) for scope in
                               (self.resource, self.service, self.spl, self.project, self.customer)]

        total_working_minutes = int((month_end - month_start).total_seconds() / 60)
        expected_total = total_working_minutes * self.price_list_item.minute_rate * self.resource.disk
        for price_estimate in price_estimates:
            message = 'Price estimate "total" is calculated wrongly for "%s". Real value: %s, expected: %s.' % (
                price_estimate.scope, price_estimate.total, expected_total)
            self.assertAlmostEqual(price_estimate.total, expected_total, msg=message)

        working_minutes = int((calculation_time - month_start).total_seconds() / 60)
        expected_consumed = working_minutes * self.price_list_item.minute_rate * self.resource.disk
        for price_estimate in price_estimates:
            message = 'Price estimate "consumed" is calculated wrongly for "%s". Real value: %s, expected: %s.' % (
                price_estimate.scope, price_estimate.consumed, expected_consumed)
            self.assertAlmostEqual(price_estimate.consumed, expected_consumed, msg=message)
