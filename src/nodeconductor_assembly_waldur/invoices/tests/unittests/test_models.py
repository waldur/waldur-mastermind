from decimal import Decimal

import datetime
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from nodeconductor_assembly_waldur.invoices import utils
from nodeconductor_assembly_waldur.invoices.tests import factories
from nodeconductor_assembly_waldur.packages.tests import fixtures, factories as package_factories


class InvoiceModelTest(TestCase):
    def setUp(self):
        self.package_fixture = fixtures.PackageFixture()

    def test_invoice_price_is_based_on_items(self):
        total = Decimal('0.00')
        with freeze_time('2016-11-04 12:00:00'):
            package_template = package_factories.PackageTemplateFactory()
            package_template.components.all().update(
                price=Decimal('0.05'),
                amount=3
            )
            hours = 24 * (utils.get_current_month_end_datetime() - timezone.now()).days
            total += hours * package_template.price
            invoice = factories.InvoiceFactory()
            self.package_fixture.customer = invoice.customer
            self.package_fixture.openstack_template = package_template
            self.package_fixture.openstack_package

            self.assertEqual(invoice.total, total)

    def test_invoice_price_changes_on_package_deletion(self):
        initial_datetime = datetime.datetime(year=2016, month=11, day=4, hour=12, minute=0, second=0)
        other_datetime = datetime.datetime(year=2016, month=11, day=25, hour=18, minute=0, second=0)

        with freeze_time(initial_datetime):
            package_template = package_factories.PackageTemplateFactory()
            package_template.components.all().update(
                price=Decimal('0.05'),
                amount=3
            )
            invoice = factories.InvoiceFactory()
            self.package_fixture.customer = invoice.customer
            self.package_fixture.openstack_template = package_template
            package = self.package_fixture.openstack_package

        with freeze_time(other_datetime):
            package.delete()
            hours = 24 * (other_datetime - initial_datetime).days
            total = hours * package_template.price

            self.assertEqual(invoice.total, total)
