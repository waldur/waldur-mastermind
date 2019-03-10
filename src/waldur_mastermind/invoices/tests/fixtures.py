from django.utils.functional import cached_property

from waldur_mastermind.packages.tests import factories as packages_factories
from waldur_mastermind.packages.tests import fixtures as packages_fixtures

from . import factories


class InvoiceFixture(packages_fixtures.PackageFixture):
    @cached_property
    def invoice(self):
        return factories.InvoiceFactory(
            customer=self.customer
        )


def create_package_template(component_price=10, component_amount=1):
    template = packages_factories.PackageTemplateFactory(name='PackageTemplate')
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
