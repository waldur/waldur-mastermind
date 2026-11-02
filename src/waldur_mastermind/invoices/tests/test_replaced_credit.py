"""What a deleted credit paid stands, and a new credit pays only what is left.

`InvoiceItem.credit` is `SET_NULL`, so a deleted credit's compensations stay on
their invoices naming no credit. A run cannot clear them, because no balance is
left to give them back to, and it used to overlook them instead: the credit
that replaced the deleted one paid the same cost again, which took the invoice
below zero (#950).
"""

from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import compensations, models, tasks
from waldur_mastermind.invoices.tests import factories, fixtures

COST = 1000
# Enough to pay the whole month.
CREDIT = 5000
# Pays only part of it.
PARTIAL_CREDIT = 300


@freeze_time("2024-01-15")
class ReplacedCreditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.customer = self.fixture.customer
        self.invoice = self.fixture.invoice
        self.item = self.add_cost(COST)

    def add_cost(self, price, **kwargs):
        return factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.fixture.project,
            resource=self.fixture.resource,
            unit_price=price,
            quantity=1,
            **kwargs,
        )

    def new_credit(self, value=CREDIT):
        return factories.CustomerCreditFactory(customer=self.customer, value=value)

    def monthly_compensation(self):
        return compensations.MonthlyCompensation(self.customer, invoice=self.invoice)

    def pay_with_deleted_credit(self, value):
        """Staff apply compensations mid-month, then delete the credit."""
        credit = self.new_credit(value)
        self.monthly_compensation().apply_compensations()
        credit.delete()

    def assert_month(self, price, credit=None, drawn=0):
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.price, price)
        if credit:
            credit.refresh_from_db()
            self.assertEqual(credit.value, CREDIT - drawn)

    def test_kept_credit_pays_once(self):
        credit = self.new_credit()
        self.monthly_compensation().apply_compensations()

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(0, credit, drawn=COST)

    def test_new_credit_does_not_pay_what_the_deleted_credit_paid(self):
        self.pay_with_deleted_credit(CREDIT)
        credit = self.new_credit()

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(0, credit, drawn=0)

    def test_new_credit_pays_the_rest_of_a_partly_paid_item(self):
        self.pay_with_deleted_credit(PARTIAL_CREDIT)
        credit = self.new_credit()

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(0, credit, drawn=COST - PARTIAL_CREDIT)

    def test_new_credit_pays_cost_added_since(self):
        self.pay_with_deleted_credit(CREDIT)
        self.add_cost(200)
        credit = self.new_credit()

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(0, credit, drawn=200)

    def test_new_credit_pays_what_a_paired_discount_leaves(self):
        self.add_cost(
            -100,
            details={"is_discount": True, "discount_of_item": self.item.uuid.hex},
        )
        self.pay_with_deleted_credit(PARTIAL_CREDIT)
        credit = self.new_credit()

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(0, credit, drawn=COST - 100 - PARTIAL_CREDIT)

    def test_new_credit_pays_what_a_project_credit_left(self):
        # A project credit is deleted with the organization credit funding it,
        # so only what it paid stays behind.
        credit = self.new_credit()
        factories.ProjectCreditFactory(
            project=self.fixture.project, value=PARTIAL_CREDIT
        )
        self.monthly_compensation().apply_compensations()
        credit.delete()
        replacement = self.new_credit()

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(0, replacement, drawn=COST - PARTIAL_CREDIT)

    def test_applying_again_gives_the_same_result(self):
        self.pay_with_deleted_credit(PARTIAL_CREDIT)
        credit = self.new_credit()

        self.monthly_compensation().apply_compensations()
        self.monthly_compensation().apply_compensations()

        self.assert_month(0, credit, drawn=COST - PARTIAL_CREDIT)
        self.assertEqual(self.invoice.items.filter(credit=credit).count(), 1)

    def test_clearing_leaves_what_the_deleted_credit_paid(self):
        self.pay_with_deleted_credit(PARTIAL_CREDIT)
        credit = self.new_credit()
        self.monthly_compensation().apply_compensations()

        self.monthly_compensation().clear_compensations()

        self.assert_month(COST - PARTIAL_CREDIT, credit, drawn=0)

    def test_compensation_without_a_replacement_stays(self):
        self.pay_with_deleted_credit(PARTIAL_CREDIT)

        tasks.process_invoice_credits(self.invoice)

        self.assert_month(COST - PARTIAL_CREDIT)

    def test_open_month_report_counts_only_what_is_left(self):
        self.pay_with_deleted_credit(PARTIAL_CREDIT)
        credit = self.new_credit()

        self.assertEqual(
            models.creditable_cost(self.invoice, credit),
            Decimal(COST - PARTIAL_CREDIT),
        )

    def test_open_month_report_keeps_what_the_credit_in_place_wrote(self):
        credit = self.new_credit()
        self.monthly_compensation().apply_compensations()

        self.assertEqual(models.creditable_cost(self.invoice, credit), Decimal(COST))
