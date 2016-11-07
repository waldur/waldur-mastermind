from django.utils.functional import cached_property

from nodeconductor_assembly_waldur.packages.tests import fixtures as packages_fixtures

from . import factories


class InvoiceFixture(packages_fixtures.PackageFixture):
    @cached_property
    def invoice(self):
        return factories.InvoiceFactory(
            customer=self.customer
        )
