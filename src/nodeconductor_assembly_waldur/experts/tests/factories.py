import factory

from rest_framework.reverse import reverse

from nodeconductor.structure.tests import factories as structure_factories

from .. import models


class ExpertProviderFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ExpertProvider

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, expert_provider=None, action=None):
        if expert_provider is None:
            expert_provider = ExpertProviderFactory()
        url = 'http://testserver' + reverse('expertprovider-detail', kwargs={'uuid': expert_provider.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('expertprovider-list')
        return url if action is None else url + action + '/'
