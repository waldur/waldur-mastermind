import datetime

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from nodeconductor_openstack.openstack.tests import factories as openstack_factories

from nodeconductor_assembly_waldur.packages.tests import factories as package_factories
from ... import utils, models
from .. import factories


class InvoiceModelTest(TestCase):
    def setUp(self):
        self.tenant = openstack_factories.TenantFactory()
        self.service_settings = self.tenant.service_project_link.service.settings
        self.package_template = package_factories.PackageTemplateFactory(
            service_settings=self.service_settings)
        self.package_template.components.all().update(
            price=Decimal('0.05'),
            amount=3,
        )

    def test_invoice_price_is_based_on_items(self):
        total = Decimal('0.00')
        with freeze_time('2016-11-04 12:00:00'):

            hours = 24 * (utils.get_current_month_end_datetime() - timezone.now()).days
            total += hours * self.package_template.price
            invoice = factories.InvoiceFactory(customer=self.tenant.service_project_link.project.customer)
            package_factories.OpenStackPackageFactory(tenant=self.tenant, template=self.package_template,
                                                      service_settings=self.service_settings)

            self.assertEqual(invoice.total, total)

    def test_invoice_price_changes_on_package_deletion(self):
        initial_datetime = datetime.datetime(year=2016, month=11, day=4, hour=12, minute=0, second=0)
        other_datetime = datetime.datetime(year=2016, month=11, day=25, hour=18, minute=0, second=0)

        with freeze_time(initial_datetime):
            invoice = factories.InvoiceFactory(customer=self.tenant.service_project_link.project.customer)
            package = package_factories.OpenStackPackageFactory(tenant=self.tenant, template=self.package_template,
                                                                service_settings=self.service_settings)

        with freeze_time(other_datetime):
            package.delete()
            total = models.OpenStackItem.calculate_price_for_period(self.package_template.price,
                                                                    initial_datetime, other_datetime)

            self.assertEqual(invoice.total, total)
