import factory

from django.core.urlresolvers import reverse
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories

from waldur_paypal import models


class PaypalPaymentFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Payment

    amount = 10
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    backend_id = factory.Sequence(lambda n: 'PAYMENT-ABC-%s' % n)
    token = factory.Sequence(lambda n: 'TOKEN-%s' % n)

    @classmethod
    def get_url(self, payment=None, action=None):
        if payment is None:
            payment = PaypalPaymentFactory()
        url = 'http://testserver' + reverse('paypal-payment-detail', kwargs={'uuid': payment.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('paypal-payment-list')


class InvoiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Invoice

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    backend_id = factory.Sequence(lambda n: 'INV2-ETBW-Q5NB-VWLT-9RH%s' % n)
    state = models.Invoice.States.DRAFT
    invoice_date = factory.fuzzy.FuzzyDate(start_date=timezone.now().date())
    month = factory.fuzzy.FuzzyInteger(1, 12)
    year = factory.fuzzy.FuzzyInteger(1970, 2017)
    end_date = factory.fuzzy.FuzzyDate(start_date=timezone.now().date())
    tax_percent = factory.fuzzy.FuzzyInteger(1, 10)
    issuer_details = factory.Dict({
        'email': factory.Sequence(lambda n: 'email-%s@domain.com' % n),
        'address': factory.Sequence(lambda n: 'address-%s' % n),
        'city': factory.Sequence(lambda n: 'city-%s' % n),
        'postal_code': 1101,
        'country_code': 'EE',
        'phone': {
            'country_code': '372',
            'national_number': '5555555'
        }
    })


class InvoiceItemFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.InvoiceItem

    invoice = factory.SubFactory(InvoiceFactory)
    price = factory.fuzzy.FuzzyInteger(10, 100)
    tax = factory.fuzzy.FuzzyInteger(1, 10)
    unit_price = factory.fuzzy.FuzzyInteger(5, 30)
    quantity = factory.fuzzy.FuzzyInteger(24, 2400, step=24)
    unit_of_measure = models.InvoiceItem.UnitsOfMeasure.HOURS
    name = factory.Sequence(lambda n: 'Invoice-item-%s' % n)
    start = factory.fuzzy.FuzzyDateTime(start_dt=timezone.now())
