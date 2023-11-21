import factory
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models


class CallManagerFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.CallManager

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, manager=None, action=None):
        if manager is None:
            manager = CallManagerFactory()
        url = 'http://testserver' + reverse(
            'proposal-call-manager-detail',
            kwargs={'uuid': manager.uuid.hex},
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('proposal-call-manager-list')
        return url if action is None else url + action + '/'
