"""A compensation that pays an earlier invoice counts in the month it pays.

A project credit can pay the uncovered cost of an earlier, closed invoice. The
compensation is written on the open invoice, but `compensated_period` names the
month it pays, and every cost-by-month figure counts it there: counted by its
invoice's month, it would take the current month's cost below zero.
"""

import datetime
import decimal

from freezegun import freeze_time
from rest_framework import status, test

from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.invoices import models, utils
from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.invoices.tests import factories, fixtures

SEPTEMBER = datetime.date(2026, 9, 1)


@freeze_time("2026-10-15")
class CompensatedPeriodTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.credit = self.fixture.customer_credit
        self.september = factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2026,
            month=9,
            state=models.Invoice.States.CREATED,
        )
        self.october = factories.InvoiceFactory(
            customer=self.fixture.customer, year=2026, month=10
        )
        self._item(self.september, 1000)
        self._item(self.october, 100)
        # September's cost, paid from credit on October's invoice.
        self.payment = self._item(
            self.october, -400, credit=self.credit, compensated_period=SEPTEMBER
        )

    def _item(self, invoice, price, **kwargs):
        return factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            resource=self.resource,
            unit_price=decimal.Decimal(price),
            quantity=1,
            **kwargs,
        )

    def test_month_q_counts_the_payment_in_the_month_it_pays(self):
        def items_in(year, month):
            return set(
                models.InvoiceItem.objects.filter(
                    utils.month_q(year, month), project=self.project
                ).values_list("unit_price", flat=True)
            )

        self.assertEqual(items_in(2026, 9), {decimal.Decimal(1000), -400})
        self.assertEqual(items_in(2026, 10), {decimal.Decimal(100)})

    def test_costs_are_grouped_by_the_month_paid(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            factories.InvoiceItemFactory.get_list_url("costs"),
            {"project_uuid": self.project.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        months = {(row["year"], row["month"]): row for row in response.data}
        self.assertEqual(months[(2026, 10)]["price"], "100.00")
        self.assertEqual(months[(2026, 10)]["compensation"], "0.00")
        self.assertEqual(months[(2026, 9)]["incurred"], "1000.00")
        self.assertEqual(months[(2026, 9)]["compensation"], "-400.00")
        self.assertEqual(months[(2026, 9)]["price"], "600.00")

    def test_current_month_cost_for_period_leaves_the_payment_out(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            factories.InvoiceItemFactory.get_list_url("project_costs_for_period"),
            {
                "project_uuid": self.project.uuid.hex,
                "period": PeriodMixin.Periods.MONTH_1,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_price"], "100.00")

    def test_three_month_cost_for_period_counts_the_payment(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            factories.InvoiceItemFactory.get_list_url("project_costs_for_period"),
            {
                "project_uuid": self.project.uuid.hex,
                "period": PeriodMixin.Periods.MONTH_3,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total_price"], "700.00")

    def test_current_month_estimate_leaves_the_payment_out(self):
        estimate, _ = billing_models.PriceEstimate.objects.get_or_create(
            scope=self.project
        )
        estimate.update_total()

        self.assertEqual(estimate.total, decimal.Decimal(100))
        self.assertEqual(
            decimal.Decimal(
                utils.get_billing_price_estimate_for_resources([self.resource])[
                    "current"
                ]
            ),
            100,
        )

    def test_consumption_last_month_leaves_the_payment_out(self):
        project_credit = self.fixture.project_credit
        # October's own compensation, next to the payment of September.
        self._item(self.october, -60, credit=self.credit)

        with freeze_time("2026-11-02"):
            self.assertEqual(self.credit.consumption_last_month, 60)
            self.assertEqual(project_credit.consumption_last_month, 60)
