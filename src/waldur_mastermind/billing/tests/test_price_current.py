from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.invoices.tests import fixtures as invoice_fixtures


@freeze_time('2017-01-10')
class PriceCurrentTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = invoice_fixtures.InvoiceFixture()

        invoice_factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,
            unit=invoice_models.InvoiceItem.Units.PER_MONTH,
            unit_price=100,
            quantity=1,
        )
        invoice_factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,
            unit=invoice_models.InvoiceItem.Units.PER_DAY,
            unit_price=3,
        )

    def test_current_price(self):
        self.client.force_authenticate(self.fixture.staff)
        url = structure_factories.CustomerFactory.get_url(self.fixture.project.customer)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['billing_price_estimate']['current'], 100 + 9 * 3)
        diff = (
            data['billing_price_estimate']['total']
            - data['billing_price_estimate']['current']
        )
        self.assertEqual(diff, (31 - 9) * 3)
