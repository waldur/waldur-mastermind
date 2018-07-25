import datetime

from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase
from freezegun import freeze_time

from waldur_core.cost_tracking import models, ConsumableItem
from waldur_core.cost_tracking.tests import factories
from waldur_core.structure.tests import factories as structure_factories


class ConsumptionDetailsTest(TransactionTestCase):

    def setUp(self):
        price_estimate = factories.PriceEstimateFactory(year=2016, month=8)
        self.consumption_details = factories.ConsumptionDetailsFactory(price_estimate=price_estimate)
        self.storage_item = ConsumableItem('storage', '1 MB')

    def test_current_configuration_update(self):
        """ Test that consumed_before_modification field stores consumed items on configuration update """
        # Resource used some consumables
        with freeze_time("2016-08-08 11:00:00"):
            old_configuration = {self.storage_item: 1024, ConsumableItem('ram', '1 MB'): 512}
            self.consumption_details.update_configuration(old_configuration)
        # After 2 hours resource configuration was updated
        with freeze_time("2016-08-08 13:00:00"):
            self.consumption_details.update_configuration({self.storage_item: 2048})
        # Details of consumed items should be stored
        HOURS_BEFORE_UPDATE = 2
        for consumable_item, usage in old_configuration.items():
            self.assertEqual(self.consumption_details.consumed_before_update[consumable_item],
                             old_configuration[consumable_item] * HOURS_BEFORE_UPDATE * 60)

    def test_cannot_update_configuration_for_previous_month(self):
        with freeze_time("2016-09-01"):
            self.assertRaises(
                models.ConsumptionDetailUpdateError,
                lambda: self.consumption_details.update_configuration({self.storage_item: 2048}))

    def test_consumed_in_month(self):
        """ Property "consumed_in_month" should return how much consumables resource will use for whole month """
        # Resource used some consumables
        start_time = datetime.datetime(2016, 8, 8, 11, 0)
        with freeze_time(start_time):
            old_configuration = {self.storage_item: 1024}
            self.consumption_details.update_configuration(old_configuration)
        # After 2 hours resource configuration was updated
        change_time = datetime.datetime(2016, 8, 8, 13, 0)
        with freeze_time(change_time):
            new_configuration = {self.storage_item: 2048}
            self.consumption_details.update_configuration(new_configuration)

        # Expected consumption should assume that resource will use current
        # configuration to the end of month and add 2 hours of old configuration
        month_end = datetime.datetime(2016, 8, 31, 23, 59, 59)
        expected = (int((change_time - start_time).total_seconds() / 60) * old_configuration[self.storage_item] +
                    int((month_end - change_time).total_seconds() / 60) * new_configuration[self.storage_item])
        self.assertEqual(self.consumption_details.consumed_in_month[self.storage_item], expected)

    def test_consumed_until_now(self):
        # Resource used some consumables
        start_time = datetime.datetime(2016, 8, 8, 11, 0)

        with freeze_time(start_time):
            old_configuration = {self.storage_item: 1024}
            self.consumption_details.update_configuration(old_configuration)
        # After two hours resource configuration was updated
        change_time = datetime.datetime(2016, 8, 8, 13, 0)
        with freeze_time(change_time):
            new_configuration = {self.storage_item: 2048}
            self.consumption_details.update_configuration(new_configuration)

        # After one more hour we are checking how much resources were consumed
        now_time = datetime.datetime(2016, 8, 8, 14, 0)
        with freeze_time(now_time):
            expected = (
                2 * 60 * old_configuration[self.storage_item] +  # Resource worked two hours with old configuration
                1 * 60 * new_configuration[self.storage_item])   # And one hour with new configuration
            self.assertEqual(self.consumption_details.consumed_until_now[self.storage_item], expected)


class PriceListItemTest(TransactionTestCase):

    def test_get_for_resource(self):
        resource = structure_factories.TestNewInstanceFactory()
        resource_content_type = ContentType.objects.get_for_model(resource)
        service = resource.service_project_link.service
        # resource has two default price list items
        default_item1 = models.DefaultPriceListItem.objects.create(
            resource_content_type=resource_content_type, item_type='flavor', key='small', value=10)
        default_item2 = models.DefaultPriceListItem.objects.create(
            resource_content_type=resource_content_type, item_type='storage', key='1 GB', value=0.5)
        # the second item is overridden be regular price list item
        item = models.PriceListItem.objects.create(default_price_list_item=default_item2, service=service)

        expected = {default_item1, item}
        self.assertSetEqual(models.PriceListItem.get_for_resource(resource), expected)


class DefaultPriceListItemTest(TransactionTestCase):

    def test_get_consumable_items_pretty_names(self):
        item_type, key = 'flavor', 'small'
        price_list_item = factories.DefaultPriceListItemFactory(item_type=item_type, key=key, name='Pretty name')
        consumable_item = ConsumableItem(item_type, key)

        expected = {consumable_item: price_list_item.name}
        actual = models.DefaultPriceListItem.get_consumable_items_pretty_names(
            price_list_item.resource_content_type, [consumable_item])
        self.assertDictEqual(actual, expected)
