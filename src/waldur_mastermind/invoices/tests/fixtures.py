from django.utils.functional import cached_property

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

from . import factories


class InvoiceFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def invoice(self):
        return factories.InvoiceFactory(customer=self.customer)

    @cached_property
    def invoice_item(self):
        return factories.InvoiceItemFactory(
            name="OFFERING-001",
            resource=self.resource,
            project=self.project,
            invoice=self.invoice,
            unit_price=10,
            quantity=30,
        )


class CreditFixture(InvoiceFixture):
    @cached_property
    def customer_credit(self):
        return factories.CustomerCreditFactory(customer=self.customer)

    @cached_property
    def project_credit(self):
        self.customer_credit
        return factories.ProjectCreditFactory(project=self.project)


class AffiliateFixture(InvoiceFixture):
    """The referred customer comes from InvoiceFixture (self.customer);
    the affiliate is a second organization with its own owner."""

    @cached_property
    def affiliate_link(self):
        return factories.CustomerAffiliateFactory(
            customer=self.customer, affiliate=self.affiliate_customer
        )

    @cached_property
    def affiliate_customer(self):
        return structure_factories.CustomerFactory()

    @cached_property
    def affiliate_owner(self):
        user = structure_factories.UserFactory()
        self.affiliate_customer.add_user(user, CustomerRole.OWNER)
        return user
