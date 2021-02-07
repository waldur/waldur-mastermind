from django.utils.functional import cached_property

from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

from . import factories


class InvoiceFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def invoice(self):
        return factories.InvoiceFactory(customer=self.customer)

    @cached_property
    def invoice_item(self):
        return factories.InvoiceItemFactory(
            name='OFFERING-001',
            project=self.project,
            invoice=self.invoice,
            unit_price=10,
        )
