import datetime

from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.cost_tracking import CostTrackingRegister, models, ConsumableItem, tasks
from waldur_core.cost_tracking.tests import factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.models import TestNewInstance


class ResourceUpdateTest(TransactionTestCase):

    def setUp(self):
        CostTrackingRegister.register_strategy(factories.TestNewInstanceCostTrackingStrategy)
        resource_content_type = ContentType.objects.get_for_model(TestNewInstance)
        self.price_list_item = models.DefaultPriceListItem.objects.create(
            item_type='storage', key='1 MB', value=0.5, resource_content_type=resource_content_type)

    @freeze_time('2016-08-08 11:00:00', tick=True)  # freeze time to avoid bugs in the end of a month.
    def test_consumption_details_of_resource_is_keeped_up_to_date(self):
        today = timezone.now()
        configuration = dict(ram=2048, disk=20 * 1024, cores=2)
        resource = structure_factories.TestNewInstanceFactory(
            state=TestNewInstance.States.OK, runtime_state='online', **configuration)

        price_estimate = models.PriceEstimate.objects.get(scope=resource, month=today.month, year=today.year)
        consumption_details = price_estimate.consumption_details
        expected = {
            ConsumableItem('ram', '1 MB'): 2048,
            ConsumableItem('storage', '1 MB'): 20 * 1024,
            ConsumableItem('cores', '1 core'): 2,
            ConsumableItem('quotas', 'test_quota'): 0,
        }
        self.assertDictEqual(consumption_details.configuration, expected)

        resource.ram = 1024
        resource.save()
        consumption_details.refresh_from_db()
        self.assertEqual(consumption_details.configuration[ConsumableItem('ram', '1 MB')], resource.ram)

        resource.runtime_state = 'offline'
        resource.save()
        # test resource uses only storage and quota when it is offline
        expected = {
            ConsumableItem('storage', '1 MB'): 20 * 1024,
            ConsumableItem('quotas', 'test_quota'): 0
        }
        consumption_details.refresh_from_db()
        self.assertDictEqual(consumption_details.configuration, expected)

        resource.flavor_name = 'small'
        resource.save()
        consumption_details.refresh_from_db()
        self.assertEqual(consumption_details.configuration[ConsumableItem('flavor', 'small')], 1)

    def test_price_estimate_of_resource_is_keeped_up_to_date(self):
        start_time = datetime.datetime(2016, 8, 8, 11, 0)
        with freeze_time(start_time):
            today = timezone.now()
            old_disk = 20 * 1024
            resource = structure_factories.TestNewInstanceFactory(
                state=TestNewInstance.States.OK, runtime_state='online', disk=old_disk)
            price_estimate = models.PriceEstimate.objects.get(scope=resource, month=today.month, year=today.year)
            # after resource creation price estimate should be calculate for whole month
            month_end = datetime.datetime(2016, 8, 31, 23, 59, 59)
            expected = (
                int((month_end - start_time).total_seconds() / 60) * self.price_list_item.minute_rate * old_disk)
            self.assertAlmostEqual(price_estimate.total, expected)

        # after some time resource disk was updated - resource price should be recalculated
        change_time = datetime.datetime(2016, 8, 9, 13, 0)
        with freeze_time(change_time):
            new_disk = 40 * 1024
            resource.disk = new_disk
            resource.save()

            price_estimate.refresh_from_db()
            expected = (
                int((change_time - start_time).total_seconds() / 60) * self.price_list_item.minute_rate * old_disk +
                int((month_end - change_time).total_seconds() / 60) * self.price_list_item.minute_rate * new_disk)
            self.assertAlmostEqual(price_estimate.total, expected)

    def test_price_estimate_of_resource_ancestors_is_keeped_up_to_date(self):
        """ On resource configuration tests handlers should update resource ancestors estimates """
        start_time = datetime.datetime(2016, 8, 8, 11, 0)
        with freeze_time(start_time):
            today = timezone.now()
            old_disk = 20 * 1024
            resource = structure_factories.TestNewInstanceFactory(
                state=TestNewInstance.States.OK, runtime_state='online', disk=old_disk)
            ancestors = [resource.service_project_link, resource.service_project_link.service,
                         resource.service_project_link.project, resource.service_project_link.project.customer]
            # after resource creation price for it ancestors should be calculate for whole month
            month_end = datetime.datetime(2016, 8, 31, 23, 59, 59)
            expected = (
                int((month_end - start_time).total_seconds() / 60) * self.price_list_item.minute_rate * old_disk)
            for ancestor in ancestors:
                ancestor_estimate = models.PriceEstimate.objects.get(scope=ancestor, month=today.month, year=today.year)
                self.assertAlmostEqual(ancestor_estimate.total, expected)

        # after some time resource disk was updated - resource ancestors price should be recalculated
        change_time = datetime.datetime(2016, 8, 9, 13, 0)
        with freeze_time(change_time):
            new_disk = 40 * 1024
            resource.disk = new_disk
            resource.save()

            expected = (
                int((change_time - start_time).total_seconds() / 60) * self.price_list_item.minute_rate * old_disk +
                int((month_end - change_time).total_seconds() / 60) * self.price_list_item.minute_rate * new_disk)
            for ancestor in ancestors:
                ancestor_estimate = models.PriceEstimate.objects.get(scope=ancestor, month=today.month, year=today.year)
                self.assertAlmostEqual(ancestor_estimate.total, expected)

    def test_historical_estimates_are_initialized(self):
        creation_time = timezone.make_aware(datetime.datetime(2016, 7, 15, 11, 0))
        import_time = timezone.make_aware(datetime.datetime(2016, 9, 2, 10, 0))
        with freeze_time(import_time):
            resource = structure_factories.TestNewInstanceFactory(
                state=TestNewInstance.States.OK, runtime_state='online', disk=20 * 1024, created=creation_time)
            ancestors = [resource.service_project_link, resource.service_project_link.service,
                         resource.service_project_link.project, resource.service_project_link.project.customer]

        # signal should init estimates for resource and its ancestors for previous months
        for scope in [resource] + ancestors:
            self.assertTrue(models.PriceEstimate.objects.filter(scope=scope, month=7, year=2016))
            self.assertTrue(models.PriceEstimate.objects.filter(scope=scope, month=8, year=2016))

        # Check price estimates total calculation for month #7
        month_end = timezone.make_aware(datetime.datetime(2016, 7, 31, 23, 59, 59))
        work_minutes = int((month_end - creation_time).total_seconds() / 60)
        expected = work_minutes * self.price_list_item.minute_rate * resource.disk
        for scope in [resource] + ancestors:
            estimate = models.PriceEstimate.objects.get(scope=scope, month=7, year=2016)
            self.assertAlmostEqual(estimate.total, expected)

        # Check price estimates total calculation for month #8
        month_start = timezone.make_aware(datetime.datetime(2016, 8, 1))
        month_end = timezone.make_aware(datetime.datetime(2016, 8, 31, 23, 59, 59))
        work_minutes = int((month_end - month_start).total_seconds() / 60)
        expected = work_minutes * self.price_list_item.minute_rate * resource.disk
        for scope in [resource] + ancestors:
            estimate = models.PriceEstimate.objects.get(scope=scope, month=8, year=2016)
            self.assertAlmostEqual(estimate.total, expected)


class ResourceQuotaUpdateTest(TransactionTestCase):

    def setUp(self):
        CostTrackingRegister.register_strategy(factories.TestNewInstanceCostTrackingStrategy)

    @freeze_time('2016-08-08 11:00:00', tick=True)  # freeze time to avoid bugs in the end of a month.
    def test_consumption_details_of_resource_is_keeped_up_to_date_on_quota_change(self):
        today = timezone.now()
        resource = structure_factories.TestNewInstanceFactory()
        quota_item = ConsumableItem('quotas', 'test_quota')

        price_estimate = models.PriceEstimate.objects.get(scope=resource, month=today.month, year=today.year)
        consumption_details = price_estimate.consumption_details
        self.assertEqual(consumption_details.configuration[quota_item], 0)

        resource.set_quota_usage(TestNewInstance.Quotas.test_quota, 5)

        consumption_details.refresh_from_db()
        self.assertEqual(consumption_details.configuration[quota_item], 5)


class ScopeDeleteTest(TransactionTestCase):

    def setUp(self):
        resource_content_type = ContentType.objects.get_for_model(TestNewInstance)
        self.price_list_item = models.DefaultPriceListItem.objects.create(
            item_type='storage', key='1 MB', resource_content_type=resource_content_type, value=0.1)
        CostTrackingRegister.register_strategy(factories.TestNewInstanceCostTrackingStrategy)
        self.start_time = datetime.datetime(2016, 8, 8, 11, 0)
        with freeze_time(self.start_time):
            self.resource = structure_factories.TestNewInstanceFactory(disk=20 * 1024)
        self.spl = self.resource.service_project_link
        self.project = self.spl.project
        self.customer = self.project.customer
        self.service = self.spl.service

    @freeze_time('2016-08-08 13:00:00')
    def test_all_estimates_are_deleted_on_customer_deletion(self):
        self.resource.delete()
        self.project.delete()
        self.customer.delete()

        for scope in (self.spl, self.resource, self.service, self.project, self.customer):
            self.assertFalse(models.PriceEstimate.objects.filter(scope=scope).exists(),
                             'Unexpected price estimate exists for %s %s' % (scope.__class__.__name__, scope))

    @freeze_time('2016-08-08 13:00:00')
    def test_estimate_is_recalculated_on_resource_deletion(self):
        self.resource.delete()

        expected = 2 * self.price_list_item.value * self.resource.disk  # resource has been working for 2 hours
        for scope in (self.spl, self.service, self.project, self.customer):
            self.assertEqual(models.PriceEstimate.objects.get_current(scope).total, expected)

    @freeze_time('2016-08-08 13:00:00')
    def test_estimate_populate_details_on_scope_deletion(self):
        scopes = (self.resource, self.project)
        for scope in scopes:
            estimate = models.PriceEstimate.objects.get_current(scope)
            scope.delete()
            estimate.refresh_from_db()

            self.assertEqual(estimate.details['name'], scope.name)

        estimate = models.PriceEstimate.objects.get_current(self.service)
        self.service.delete()
        estimate.refresh_from_db()
        self.assertEqual(estimate.details['name'], str(self.service))

    # set time to next month to make sure that estimates for previous months are deleted too.
    @freeze_time('2016-09-01 12:00:00')
    def test_estimated_is_removed_on_resource_unlink(self):
        tasks.recalculate_estimate()  # recalculate to create new estimates in new month.

        self.resource.unlink()
        self.resource.delete()

        self.assertFalse(models.PriceEstimate.objects.filter(scope=self.resource).exists())
        details_names = [estimate.details.get('name') for estimate in models.PriceEstimate.objects.all()]
        self.assertNotIn(self.resource.name, details_names)

        for scope in (self.spl, self.service, self.project, self.customer):
            for price_estimate in models.PriceEstimate.objects.filter(scope=scope):
                self.assertEqual(price_estimate.total, 0)
