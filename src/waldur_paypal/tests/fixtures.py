from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories


class PayPalFixture(structure_fixtures.CustomerFixture):

    @cached_property
    def payment(self):
        return factories.PaypalPaymentFactory(customer=self.customer)

    @cached_property
    def invoice(self):
        return factories.InvoiceFactory(customer=self.customer)
