from datetime import datetime
from freezegun import freeze_time
from rest_framework import test, status

from nodeconductor_assembly_waldur.packages.tests import fixtures as package_fixtures, factories as package_factories
from nodeconductor_assembly_waldur.packages import models as package_models

from ... import models, tasks


class InvoicePriceWorkflowTest(test.APITransactionTestCase):
    url = package_factories.OpenStackPackageFactory.get_list_url()

    def setUp(self):
        self.fixture = package_fixtures.PackageFixture()

    def get_package_create_payload(self):
        spl = self.fixture.openstack_spl
        spl_url = package_factories.OpenStackServiceProjectLinkFactory.get_url(spl)
        template = package_factories.PackageTemplateFactory(service_settings=spl.service.settings)
        return {
            'service_project_link': spl_url,
            'name': 'test_package',
            'template': package_factories.PackageTemplateFactory.get_url(template),
        }

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

        middle_of_the_month = datetime(2017, 1, 15)
        with freeze_time(middle_of_the_month):
            payload = self.get_package_create_payload()
            response = self.client.post(self.url, data=payload)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
            self.assertEqual(models.Invoice.objects.count(), 1)

        template = package_models.PackageTemplate.objects.first()
        price_per_day = template.price
        end_of_the_month = datetime(2017, 1, 31)
        expected_price = (end_of_the_month - middle_of_the_month).days * price_per_day
        with freeze_time(end_of_the_month):
            invoice = models.Invoice.objects.first()
            self.assertEqual(invoice.price, expected_price)

        beginning_of_the_new_month = datetime(2017, 2, 1)
        task_triggering_date = datetime(2017, 2, 2)
        expected_price = (task_triggering_date - beginning_of_the_new_month).days * price_per_day
        with freeze_time(task_triggering_date):
            tasks.create_monthly_invoices_for_packages()
            self.assertEqual(models.Invoice.objects.count(), 2)

            invoice.refresh_from_db()
            second_invoice = models.Invoice.objects.exclude(pk=invoice.pk).first()
            self.assertEqual(second_invoice.price, expected_price)

            self.assertEqual(invoice.state, models.Invoice.States.CREATED)
            self.assertEqual(invoice.invoice_date, datetime.now().date())
            self.assertEqual(second_invoice.state, models.Invoice.States.PENDING)
            self.assertIsNone(second_invoice.invoice_date)

        package_deletion_date = datetime(2017, 2, 20)
        expected_price = (package_deletion_date - beginning_of_the_new_month).days * price_per_day
        with freeze_time(package_deletion_date):
            package = package_models.OpenStackPackage.objects.first()
            package.delete()

        week_after_deletion = datetime(2017, 2, 27)
        with freeze_time(week_after_deletion):
            second_invoice.refresh_from_db()
            self.assertEqual(expected_price, second_invoice.price)
            openstack_item = second_invoice.openstack_items.first()
            self.assertEqual(openstack_item.end.date(), package_deletion_date.date())

