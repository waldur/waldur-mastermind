from decimal import Decimal

import factory
from django.db.models import signals
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common.mixins import UnitPriceMixin
from .. import models


OFFERING_OPTIONS = {
    'order': ['storage', 'ram', 'cpu_count'],
    'options': {
        'storage': {
            'type': 'integer',
            'label': 'Max storage, GB',
            'required': True,
            'help_text': 'VPC storage limit in GB.',
        },
        'ram': {
            'type': 'integer',
            'label': 'Max RAM, GB',
            'required': True,
            'help_text': 'VPC RAM limit in GB.',
        },
        'cpu_count': {
            'type': 'integer',
            'label': 'Max vCPU',
            'required': True,
            'help_text': 'VPC CPU count limit.',
        },
    },
}


class ServiceProviderFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ServiceProvider

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, service_provider=None, action=None):
        if service_provider is None:
            service_provider = ServiceProviderFactory()
        url = 'http://testserver' + reverse('marketplace-service-provider-detail',
                                            kwargs={'uuid': service_provider.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-service-provider-list')
        return url if action is None else url + action + '/'


class CategoryFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Category

    title = factory.Sequence(lambda n: 'category-%s' % n)

    @classmethod
    def get_url(cls, category=None, action=None):
        if category is None:
            category = CategoryFactory()
        url = 'http://testserver' + reverse('marketplace-category-detail',
                                            kwargs={'uuid': category.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-category-list')
        return url if action is None else url + action + '/'


class OfferingFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Offering

    name = factory.Sequence(lambda n: 'offering-%s' % n)
    category = factory.SubFactory(CategoryFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, offering=None, action=None):
        if offering is None:
            offering = OfferingFactory()
        url = 'http://testserver' + reverse('marketplace-offering-detail',
                                            kwargs={'uuid': offering.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-offering-list')
        return url if action is None else url + action + '/'


class SectionFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Section

    key = factory.Sequence(lambda n: 'section-%s' % n)
    category = factory.SubFactory(CategoryFactory)


class AttributeFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Attribute

    key = factory.Sequence(lambda n: 'attribute-%s' % n)
    section = factory.SubFactory(SectionFactory)


@factory.django.mute_signals(signals.pre_save, signals.post_save)
class ScreenshotFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Screenshot

    name = factory.Sequence(lambda n: 'screenshot-%s' % n)
    image = factory.django.ImageField()
    offering = factory.SubFactory(OfferingFactory)

    @classmethod
    def get_url(cls, screenshot=None, action=None):
        if screenshot is None:
            screenshot = ScreenshotFactory()
        url = 'http://testserver' + reverse('marketplace-screenshot-detail',
                                            kwargs={'uuid': screenshot.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-screenshot-list')
        return url if action is None else url + action + '/'


class OrderFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Order

    created_by = factory.SubFactory(structure_factories.UserFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, order=None, action=None):
        if order is None:
            order = OrderFactory()
        url = 'http://testserver' + reverse('marketplace-order-detail',
                                            kwargs={'uuid': order.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-order-list')
        return url if action is None else url + action + '/'


class OrderItemFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OrderItem

    order = factory.SubFactory(OrderFactory)
    offering = factory.SubFactory(OfferingFactory)


class PlanFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Plan

    offering = factory.SubFactory(OfferingFactory)
    name = factory.Sequence(lambda n: 'plan-%s' % n)
    unit_price = Decimal(100)
    unit = UnitPriceMixin.Units.QUANTITY

    @classmethod
    def get_url(cls, plan=None, action=None):
        if plan is None:
            plan = PlanFactory()
        url = 'http://testserver' + reverse('marketplace-plan-detail',
                                            kwargs={'uuid': plan.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('marketplace-plan-list')
        return url if action is None else url + action + '/'
