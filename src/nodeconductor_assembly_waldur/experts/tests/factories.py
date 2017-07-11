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
        url = 'http://testserver' + reverse('expert-provider-detail', kwargs={'uuid': expert_provider.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('expert-provider-list')
        return url if action is None else url + action + '/'


class ExpertRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ExpertRequest

    project = factory.SubFactory(structure_factories.ProjectFactory)
    user = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, expert_request=None, action=None):
        if expert_request is None:
            expert_request = ExpertRequestFactory()
        url = 'http://testserver' + reverse('expert-request-detail', kwargs={'uuid': expert_request.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('expert-request-list')
        return url if action is None else url + action + '/'


class ExpertBidFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.ExpertBid

    request = factory.SubFactory(ExpertRequestFactory)
    team = factory.SubFactory(structure_factories.ProjectFactory)
    user = factory.SubFactory(structure_factories.UserFactory)

    @classmethod
    def get_url(cls, expert_bid=None, action=None):
        if expert_bid is None:
            expert_bid = ExpertBidFactory()
        url = 'http://testserver' + reverse('expert-bid-detail', kwargs={'uuid': expert_bid.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('expert-bid-list')
        return url if action is None else url + action + '/'
