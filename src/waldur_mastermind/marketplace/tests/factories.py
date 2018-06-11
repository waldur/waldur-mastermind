import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


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
