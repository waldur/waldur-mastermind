from django.utils.functional import cached_property

from nodeconductor.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests import fixtures as packages_fixtures

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


def create_package_template(component_price=10, component_amount=1):
    template = packages_factories.PackageTemplateFactory()
    template.components.update(
        price=component_price,
        amount=component_amount,
    )
    return template


def create_package(component_price, tenant=None):
    template = create_package_template(component_price=component_price)
    if not tenant:
        tenant = packages_factories.TenantFactory()

    package = packages_factories.OpenStackPackageFactory(template=template, tenant=tenant)
    return package
