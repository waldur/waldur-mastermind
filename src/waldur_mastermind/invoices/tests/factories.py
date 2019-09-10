import factory
from django.utils import timezone
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class InvoiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Invoice

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    invoice_date = factory.fuzzy.FuzzyDateTime(start_dt=timezone.now())

    @classmethod
    def get_url(cls, invoice=None, action=None):
        if invoice is None:
            invoice = InvoiceFactory()
        url = 'http://testserver' + reverse('invoice-detail', kwargs={'uuid': invoice.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('invoice-list')


class InvoiceItemFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.InvoiceItem

    invoice = factory.SubFactory(InvoiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
