import factory

from rest_framework.reverse import reverse

from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor_assembly_waldur.packages.tests import factories as packages_factories

from .. import models


class InvoiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Invoice

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, invoice=None, action=None):
        if invoice is None:
            invoice = InvoiceFactory()
        url = 'http://testserver' + reverse('invoice-detail', kwargs={'uuid': invoice.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('invoice-list')


class PaymentDetailsFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.PaymentDetails

    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, payment_details=None, action=None):
        if payment_details is None:
            payment_details = PaymentDetailsFactory()
        url = 'http://testserver' + reverse('payment-details-detail', kwargs={'uuid': payment_details.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('payment-details-list')


class OpenStackItemFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OpenStackItem

    price = 0
    invoice = factory.SubFactory(InvoiceFactory)
    package = factory.SubFactory(packages_factories.OpenStackPackageFactory)
