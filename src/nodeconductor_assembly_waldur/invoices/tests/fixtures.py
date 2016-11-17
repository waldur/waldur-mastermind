from django.utils.functional import cached_property

from nodeconductor.structure.tests import fixtures as structure_fixtures
from nodeconductor_assembly_waldur.packages.tests import fixtures as packages_fixtures

from . import factories


class InvoiceFixture(packages_fixtures.PackageFixture):
    @cached_property
    def invoice(self):
        return factories.InvoiceFactory(
            customer=self.customer
        )


class PaymentDetailsFixture(structure_fixtures.ProjectFixture):
    @cached_property
    def payment_details(self):
        return factories.PaymentDetailsFactory(
            customer=self.customer
        )
