from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
import factory

from waldur_core.cost_tracking import models, CostTrackingStrategy, ConsumableItem
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import models as test_models


class PriceEstimateFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PriceEstimate

    scope = factory.SubFactory(structure_factories.ProjectFactory)
    total = factory.Iterator([10, 100, 1000, 10000, 980, 42])
    month = factory.Iterator(range(1, 13))
    year = factory.Iterator(range(2012, 2016))

    @classmethod
    def get_list_url(self, action=None):
        url = 'http://testserver' + reverse('priceestimate-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_url(cls, price_estimate, action=None):
        if price_estimate is None:
            price_estimate = PriceEstimateFactory()
        url = 'http://testserver' + reverse('priceestimate-detail', kwargs={'uuid': price_estimate.uuid})
        return url if action is None else url + action + '/'


class ConsumptionDetailsFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.ConsumptionDetails

    price_estimate = factory.SubFactory(PriceEstimateFactory)


class AbstractPriceListItemFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.AbstractPriceListItem
        abstract = True

    value = factory.Iterator([10, 100, 1000, 10000, 1313, 13])
    units = factory.Iterator(['USD', 'EUR', 'UAH', 'OMR'])


class DefaultPriceListItemFactory(AbstractPriceListItemFactory):
    class Meta(object):
        model = models.DefaultPriceListItem

    resource_content_type = factory.LazyAttribute(
        lambda _: ContentType.objects.get_for_model(test_models.TestNewInstance))

    key = factory.Sequence(lambda n: 'price list item %s' % n)
    item_type = factory.Iterator(['flavor', 'storage'])

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('defaultpricelistitem-list')

    @classmethod
    def get_url(cls, default_price_list_item, action=None):
        if default_price_list_item is None:
            default_price_list_item = DefaultPriceListItemFactory()
        url = 'http://testserver' + reverse(
            'defaultpricelistitem-detail', kwargs={'uuid': default_price_list_item.uuid})
        return url if action is None else url + action + '/'


class PriceListItemFactory(AbstractPriceListItemFactory):
    class Meta(object):
        model = models.PriceListItem

    service = factory.SubFactory(structure_factories.TestServiceFactory)
    default_price_list_item = factory.SubFactory(DefaultPriceListItemFactory)

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('pricelistitem-list')

    @classmethod
    def get_url(cls, price_list_item, action=None):
        if price_list_item is None:
            price_list_item = PriceListItemFactory()
        url = 'http://testserver' + reverse('pricelistitem-detail', kwargs={'uuid': price_list_item.uuid})
        return url if action is None else url + action + '/'


class TestNewInstanceCostTrackingStrategy(CostTrackingStrategy):
    resource_class = test_models.TestNewInstance

    class Types(object):
        STORAGE = 'storage'
        RAM = 'ram'
        CORES = 'cores'
        QUOTAS = 'quotas'
        FLAVOR = 'flavor'

    @classmethod
    def get_configuration(cls, resource):
        States = test_models.TestNewInstance.States
        if resource.state == States.ERRED:
            return {}
        resource_quota_usage = resource.quotas.get(name=test_models.TestNewInstance.Quotas.test_quota).usage
        consumables = {
            ConsumableItem(item_type=cls.Types.STORAGE, key='1 MB'): resource.disk,
            ConsumableItem(item_type=cls.Types.QUOTAS, key='test_quota'): resource_quota_usage,
        }
        if resource.runtime_state == 'online':
            consumables.update({
                ConsumableItem(item_type=cls.Types.RAM, key='1 MB'): resource.ram,
                ConsumableItem(item_type=cls.Types.CORES, key='1 core'): resource.cores,
            })
        if resource.flavor_name:
            consumables[ConsumableItem(item_type=cls.Types.FLAVOR, key=resource.flavor_name)] = 1
        return consumables

    @classmethod
    def get_consumable_items(cls):
        return [
            ConsumableItem(cls.Types.STORAGE, "1 MB", units='MB', name='Storage'),
            ConsumableItem(cls.Types.RAM, "1 MB", units='MB', name='RAM', default_price=1),
            ConsumableItem(cls.Types.CORES, "1 core", name='Cores'),
            ConsumableItem(cls.Types.QUOTAS, "test_quota", name='Test quota'),
            ConsumableItem(cls.Types.FLAVOR, "small", name='Small flavor'),
            ConsumableItem(cls.Types.FLAVOR, "medium", name='Medium flavor'),
            ConsumableItem(cls.Types.FLAVOR, "large", name='Large flavor'),
        ]
