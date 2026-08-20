"""Drawdown recorded per credit, with the floor draw kept separate.

A month of compensation moves a balance twice: once against real usage, and
once to top the draw up to the minimal-consumption floor. Only the first
produces an invoice item, so anything reconstructing history from invoice items
sees roughly half the money — which is why "Lost" could never be non-zero on
real data. These tests pin the ledger as the record that does see both.
"""

import datetime
from decimal import Decimal
from unittest import mock

from django.test import TransactionTestCase
from freezegun import freeze_time

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import compensations, ledger, models
from waldur_mastermind.invoices.tests import factories, fixtures


@freeze_time("2024-01-15")
class ProjectCreditDrawdownTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer = self.fixture.customer
        self.invoice = self.fixture.invoice
        self.invoice_item = self.fixture.invoice_item
        self.usage = self.invoice_item.price
        # A project allocation cannot exist without an organization credit
        # behind it, and compensation is capped by that balance — so it is
        # funded well clear of the amounts under test.
        self.customer_credit = self.fixture.customer_credit
        self.customer_credit.value = self.usage * 100
        self.customer_credit.save(update_fields=["value"])

    def compensate(self):
        compensations.MonthlyCompensation(self.customer).apply_compensations()

    def rows(self, credit, transaction_type=None):
        queryset = credit.transactions.all()
        if transaction_type:
            queryset = queryset.filter(transaction_type=transaction_type)
        return list(queryset)

    def total(self, credit, transaction_type):
        return sum(row.amount for row in self.rows(credit, transaction_type))

    def test_usage_and_floor_draws_are_separate_rows(self):
        # A floor well above the month's usage: the difference is taken from the
        # balance without an invoice item to show for it.
        floor = self.usage * 3
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=floor * 10,
            expected_consumption=floor,
            grace_coefficient=0,
        )
        opening = credit.value

        self.compensate()
        credit.refresh_from_db()

        compensation = self.total(credit, models.CreditTransaction.Types.COMPENSATION)
        minimal_draw = self.total(credit, models.CreditTransaction.Types.MINIMAL_DRAW)

        self.assertEqual(compensation, -self.usage)
        self.assertEqual(minimal_draw, -(floor - self.usage))
        # The two rows account for the whole movement, which is the property
        # that lets the dashboard reconcile used + lost + remaining to granted.
        self.assertEqual(opening + compensation + minimal_draw, credit.value)

    def test_a_month_that_meets_its_floor_records_no_forfeiture(self):
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=self.usage * 10,
            expected_consumption=self.usage,
            grace_coefficient=0,
        )

        self.compensate()

        self.assertEqual(
            self.total(credit, models.CreditTransaction.Types.COMPENSATION),
            -self.usage,
        )
        self.assertEqual(
            self.rows(credit, models.CreditTransaction.Types.MINIMAL_DRAW), []
        )

    def test_rows_carry_the_billing_period_and_project(self):
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=self.usage * 10,
            expected_consumption=self.usage,
            grace_coefficient=0,
        )

        self.compensate()
        # The allocation itself is a ledger row too (the grant), and it has no
        # billing period — the drawdown rows are the ones under test.
        row = self.rows(credit, models.CreditTransaction.Types.COMPENSATION)[0]

        # A real column, not the GenericForeignKey reference: the dashboard
        # groups drawdown by month in SQL.
        self.assertEqual(row.billing_period.year, self.invoice.year)
        self.assertEqual(row.billing_period.month, self.invoice.month)
        self.assertEqual(row.billing_period.day, 1)
        self.assertEqual(row.project_uuid, self.fixture.project.uuid.hex)
        self.assertEqual(row.project_name, self.fixture.project.name)

    def test_attribution_survives_the_allocation(self):
        # A ledger that loses its attribution when the allocation goes cannot
        # answer "where did the credit go". ProjectCredit is deleted outright
        # with its project (Project itself is only soft-deleted), so the ledger
        # keeps the project on the row rather than through the FK.
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=self.usage * 10,
            expected_consumption=self.usage,
            grace_coefficient=0,
        )
        self.compensate()
        project_uuid = self.fixture.project.uuid.hex

        credit.delete()

        rows = models.CreditTransaction.objects.filter(project_uuid=project_uuid)
        self.assertTrue(rows.exists())
        self.assertEqual(rows.first().project_name, self.fixture.project.name)
        self.assertIsNone(rows.first().project_credit_id)

    def test_organization_balance_records_its_own_floor_draw(self):
        # The customer tail reduces the organization pool; the project tail does
        # not. The two are separate figures by design, so they are separate rows.
        self.customer_credit.expected_consumption = self.usage * 4
        self.customer_credit.grace_coefficient = 0
        self.customer_credit.save()

        self.compensate()
        credit = self.customer_credit

        self.assertEqual(
            self.total(credit, models.CreditTransaction.Types.COMPENSATION),
            -self.usage,
        )
        self.assertEqual(
            self.total(credit, models.CreditTransaction.Types.MINIMAL_DRAW),
            -(self.usage * 4 - self.usage),
        )

    def test_reapplying_a_month_does_not_double_count_it(self):
        # apply_compensations is clear_compensations + save, and staff can run
        # it repeatedly against a pending invoice. The rollback has to land in
        # the month it reverses, or that month keeps every superseded run in its
        # total and the dashboard reports the drawdown once per run.
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project, value=self.usage * 10
        )
        opening = credit.value
        period = datetime.date(self.invoice.year, self.invoice.month, 1)

        self.compensate()
        self.compensate()
        credit.refresh_from_db()
        self.customer_credit.refresh_from_db()

        for balance, opening_value in (
            (credit, opening),
            (self.customer_credit, self.usage * 100),
        ):
            drawn = sum(
                row.amount for row in balance.transactions.filter(billing_period=period)
            )
            self.assertEqual(drawn, balance.value - opening_value)

    def test_a_refused_breakdown_is_not_filed_as_a_grant(self):
        # The handler refuses a breakdown that does not add up to the delta it
        # claims to explain and records the movement as one row instead. That
        # row must still say what the movement was: a compensation run whose
        # apportionment could not be trusted is not a grant of credit, and
        # filing it as one moves drawdown into the granted total.
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project, value=self.usage * 10
        )
        opening = credit.value
        honest_parts = compensations.MonthlyCompensation._ledger_parts

        def overstated_parts(instance):
            parts = honest_parts(instance)
            for declared in parts.values():
                declared.append(
                    ledger.TransactionPart(
                        models.CreditTransaction.Types.COMPENSATION, Decimal("1")
                    )
                )
            return parts

        with mock.patch.object(
            compensations.MonthlyCompensation, "_ledger_parts", overstated_parts
        ):
            self.compensate()
        credit.refresh_from_db()

        (row,) = self.rows(credit, models.CreditTransaction.Types.COMPENSATION)
        self.assertEqual(row.amount, credit.value - opening)
        self.assertEqual(row.reference, self.invoice)
        self.assertEqual(
            row.billing_period,
            datetime.date(self.invoice.year, self.invoice.month, 1),
        )
        self.assertEqual(
            self.rows(credit, models.CreditTransaction.Types.MINIMAL_DRAW), []
        )

    def test_a_credit_with_no_open_invoice_is_left_alone(self):
        # There is no month to bill and nothing to draw. Reached whenever staff
        # apply compensations for an organization whose invoice was already
        # finalized, which is a no-op rather than an error.
        customer = structure_factories.CustomerFactory()
        credit = factories.CustomerCreditFactory(customer=customer, value=Decimal("10"))

        compensations.MonthlyCompensation(customer).apply_compensations()

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("10"))

    def test_untyped_writes_are_still_recorded(self):
        credit = factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("100")
        )
        credit.value = Decimal("150")
        credit.save(update_fields=["value"])

        # Ordered by id, not by `created`: the test clock is frozen, so the
        # grant and this write share a timestamp.
        row = credit.transactions.order_by("id").last()
        self.assertEqual(row.amount, Decimal("50"))
        self.assertEqual(
            row.transaction_type, models.CreditTransaction.Types.STAFF_GRANT
        )
