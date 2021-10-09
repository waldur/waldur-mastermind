import decimal

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.billing import models
from waldur_mastermind.invoices.tests import factories as invoice_factories


class PriceEstimateSignalsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def test_price_estimate_is_created_for_customer_by_signal(self):
        self.assertTrue(
            models.PriceEstimate.objects.filter(scope=self.fixture.customer).exists()
        )

    def test_price_estimate_is_created_for_project_by_signal(self):
        self.assertTrue(
            models.PriceEstimate.objects.filter(scope=self.fixture.project).exists()
        )

    def test_price_estimate_is_deleted_for_customer_by_signal(self):
        self.fixture.customer.delete()
        self.assertFalse(
            models.PriceEstimate.objects.filter(scope=self.fixture.customer).exists()
        )

    def test_price_estimate_is_deleted_for_project_by_signal(self):
        self.fixture.project.delete()
        self.assertFalse(
            models.PriceEstimate.objects.filter(scope=self.fixture.project).exists()
        )

    def test_price_estimate_for_project_is_updated_when_invoice_is_created(self):
        estimate = models.PriceEstimate.objects.get(scope=self.fixture.project)
        estimate.total = 100
        estimate.save()

        invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        estimate.refresh_from_db()
        self.assertEqual(estimate.total, 0)

    def test_price_estimate_for_customer_is_updated_when_invoice_is_created(self):
        estimate = models.PriceEstimate.objects.get(scope=self.fixture.customer)
        estimate.total = 100
        estimate.save()

        invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        estimate.refresh_from_db()
        self.assertEqual(estimate.total, 0)


@ddt
class PriceEstimateAPITest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @freeze_time('2017-11-01')
    def test_get_archive_price_estimate_for_customer(self):
        self.client.force_authenticate(getattr(self.fixture, 'staff'))
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(
            total=100
        )

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 100)

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer),
            {'year': 2017, 'month': 10},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 0)

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_customer(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.customer).update(
            total=100
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(
            structure_factories.CustomerFactory.get_url(self.fixture.customer)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 100)

    @data('staff', 'owner', 'manager', 'admin')
    def test_authorized_can_get_price_estimate_for_project(self, user):
        models.PriceEstimate.objects.filter(scope=self.fixture.project).update(
            total=100
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(
            structure_factories.ProjectFactory.get_url(self.fixture.project)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        estimate = response.data['billing_price_estimate']
        self.assertEqual(estimate['total'], 100)


@ddt
@freeze_time('2017-01-01')
class PriceEstimateInvoiceItemTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data('project', 'customer')
    def test_when_invoice_item_is_created_total_is_updated(self, scope):
        invoice = invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        invoice_factories.InvoiceItemFactory(
            invoice=invoice, project=self.fixture.project, unit_price=10, quantity=31
        )
        estimate = models.PriceEstimate.objects.get(scope=getattr(self.fixture, scope))
        self.assertAlmostEqual(
            decimal.Decimal(estimate.total), decimal.Decimal(10 * 31),
        )

    @data('project', 'customer')
    def test_when_invoice_item_is_updated_total_is_updated_too(self, scope):
        invoice = invoice_factories.InvoiceFactory(customer=self.fixture.customer)
        invoice_item = invoice_factories.InvoiceItemFactory(
            invoice=invoice, project=self.fixture.project, unit_price=10, quantity=31
        )
        invoice_item.unit_price = 11
        invoice_item.save(update_fields=['unit_price'])
        estimate = models.PriceEstimate.objects.get(scope=getattr(self.fixture, scope))
        self.assertAlmostEqual(
            decimal.Decimal(estimate.total), decimal.Decimal(11 * 31),
        )
