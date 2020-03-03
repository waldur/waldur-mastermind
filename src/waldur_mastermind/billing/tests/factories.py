import factory
from django.urls import reverse
from factory import fuzzy

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class PriceEstimateFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.PriceEstimate

    scope = factory.SubFactory(structure_factories.ProjectFactory)
    total = factory.Iterator([10, 100, 1000, 10000, 980, 42])
    limit = -1
    threshold = fuzzy.FuzzyInteger(0, 1000, step=10)

    @classmethod
    def get_list_url(self, action=None):
        url = 'http://testserver' + reverse('billing-price-estimate-list')
        return url if action is None else url + action + '/'

    @classmethod
    def get_url(self, price_estimate, action=None):
        if price_estimate is None:
            price_estimate = PriceEstimateFactory()
        url = 'http://testserver' + reverse(
            'billing-price-estimate-detail', kwargs={'uuid': price_estimate.uuid.hex}
        )
        return url if action is None else url + action + '/'
