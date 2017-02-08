from datetime import datetime
from freezegun import freeze_time
from rest_framework import test, status
import pytz

from django.utils import timezone

from nodeconductor.core import utils as core_utils
from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor_assembly_waldur.packages.tests import fixtures as package_fixtures, factories as packages_factories
from nodeconductor_assembly_waldur.packages import models as package_models

from ... import models, tasks, utils


class InvoicePriceWorkflowTest(test.APITransactionTestCase):
    url = packages_factories.OpenStackPackageFactory.get_list_url()

    def setUp(self):
        self.fixture = package_fixtures.PackageFixture()

    def get_package_create_payload(self):
        spl = self.fixture.openstack_spl
        spl_url = packages_factories.OpenStackServiceProjectLinkFactory.get_url(spl)
        template = packages_factories.PackageTemplateFactory(service_settings=spl.service.settings)
        return {
            'service_project_link': spl_url,
            'name': 'test_package',
            'template': packages_factories.PackageTemplateFactory.get_url(template),
        }

    def create_package_template(self, component_price=10, component_amount=1):
        template = packages_factories.PackageTemplateFactory()
        template.components.update(
            price=component_price,
            amount=component_amount,
        )
        return template

    def test_new_invoice_is_created_in_new_month_after_half_month_of_usage(self):
        """
        Tests that invoices are created and updated accordingle to the current state of customer's package.
        Steps:
            - Test that invoice has been created;
            - Check price of it in the end of the month;
            - Ensure that a new invoice has been generated in the new month;
            - Assert that end date of newly created openstack item set to the date of package deletion.
        :return:
        """
        self.client.force_authenticate(user=self.fixture.staff)

        middle_of_the_month = datetime(2017, 1, 15, tzinfo=pytz.UTC)
        with freeze_time(middle_of_the_month):
            payload = self.get_package_create_payload()
            response = self.client.post(self.url, data=payload)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
            self.assertEqual(models.Invoice.objects.count(), 1)

        template = package_models.PackageTemplate.objects.first()
        price_per_day = template.price
        end_of_the_month = datetime(2017, 1, 31, 23, 59, 59, tzinfo=pytz.UTC)
        expected_price = utils.get_full_days(middle_of_the_month, end_of_the_month) * price_per_day
        with freeze_time(end_of_the_month):
            invoice = models.Invoice.objects.first()
            self.assertEqual(invoice.price, expected_price)

        beginning_of_the_new_month = datetime(2017, 2, 1, tzinfo=pytz.UTC)
        task_triggering_date = datetime(2017, 2, 2, 23, 59, 59, tzinfo=pytz.UTC)
        end_of_the_new_month = core_utils.month_end(beginning_of_the_new_month)
        expected_price = utils.get_full_days(beginning_of_the_new_month, end_of_the_new_month) * price_per_day
        with freeze_time(task_triggering_date):
            tasks.create_monthly_invoices()
            self.assertEqual(models.Invoice.objects.count(), 2)

            invoice.refresh_from_db()
            second_invoice = models.Invoice.objects.exclude(pk=invoice.pk).first()
            self.assertEqual(second_invoice.price, expected_price)

            self.assertEqual(invoice.state, models.Invoice.States.CREATED)
            self.assertEqual(invoice.invoice_date, datetime.now().date())
            self.assertEqual(second_invoice.state, models.Invoice.States.PENDING)
            self.assertIsNone(second_invoice.invoice_date)

        package_deletion_date = datetime(2017, 2, 20, tzinfo=pytz.UTC)
        expected_price = (package_deletion_date - beginning_of_the_new_month).days * price_per_day
        with freeze_time(package_deletion_date):
            package = package_models.OpenStackPackage.objects.first()
            package.delete()

        week_after_deletion = datetime(2017, 2, 27, tzinfo=pytz.UTC)
        with freeze_time(week_after_deletion):
            second_invoice.refresh_from_db()
            self.assertEqual(expected_price, second_invoice.price)
            openstack_item = second_invoice.openstack_items.first()
            self.assertEqual(openstack_item.end.date(), package_deletion_date.date())

    def test_package_price_is_calculated_properly_if_it_was_used_only_for_one_day(self):
        cheap_package_template = self.create_package_template(component_price=10)
        medium_package_template = self.create_package_template(component_price=15)
        expensive_package_template = self.create_package_template(component_price=20)
        customer = structure_factories.CustomerFactory()
        date = timezone.datetime(2017, 1, 26)
        month_end = timezone.datetime(2017, 1, 31, 23, 59, 59)
        full_days = utils.get_full_days(date, month_end)

        # at first user has bought cheap package
        with freeze_time(date):
            cheap_package = packages_factories.OpenStackPackageFactory(
                template=cheap_package_template,
                tenant__service_project_link__project__customer=customer)
        invoice = models.Invoice.objects.get(customer=customer)
        cheap_item = invoice.openstack_items.get(package=cheap_package)
        self.assertEqual(cheap_item.daily_price, cheap_package_template.price)
        self.assertEqual(cheap_item.usage_days, full_days)

        # later at the same day he switched to the expensive one
        with freeze_time(date + timezone.timedelta(hours=2)):
            cheap_package.delete()
            expensive_package = packages_factories.OpenStackPackageFactory(
                template=expensive_package_template, tenant=cheap_package.tenant)
        expensive_item = invoice.openstack_items.get(package=expensive_package)
        self.assertEqual(expensive_item.daily_price, expensive_package_template.price)
        self.assertEqual(expensive_item.usage_days, full_days)
        # cheap item price should become 0, because it was replaced by expensive one
        cheap_item.refresh_from_db()
        self.assertEqual(cheap_item.price, 0)
        self.assertEqual(cheap_item.usage_days, 0)

        # at last he switched to the medium one
        with freeze_time(date + timezone.timedelta(hours=4)):
            expensive_package.delete()
            medium_package = packages_factories.OpenStackPackageFactory(
                template=medium_package_template, tenant=expensive_package.tenant)
        medium_item = invoice.openstack_items.get(package=medium_package)
        # medium item usage days should start from tomorrow,
        # because expensive item should be calculated for current day
        self.assertEqual(medium_item.usage_days, full_days - 1)
        # expensive item should be calculated for one day
        expensive_item.refresh_from_db()

        # cheap item price should remain zero
        cheap_item.refresh_from_db()
        self.assertEqual(cheap_item.usage_days, 0)
        self.assertEqual(cheap_item.price, 0)
