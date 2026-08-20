"""Reconstructing drawdown for deployments that billed before the ledger.

The ledger starts recording the day it is deployed, so every existing
installation has months of drawdown behind it that nothing wrote down. These
tests bill months through the real compensation flow, delete what the ledger
recorded — standing in for a history it never saw — and check the command puts
the same figures back from invoice items and audit events alone.

Two properties matter more than the individual amounts: the reconstruction must
reconcile (`granted = used + lost + remaining`), and it must never count a month
the ledger already has.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TransactionTestCase
from freezegun import freeze_time

from waldur_core.logging import models as logging_models
from waldur_mastermind.invoices import compensations, models
from waldur_mastermind.invoices.tests import factories, fixtures

TYPES = models.CreditTransaction.Types


class BackfillCreditLedgerTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        # A project allocation is capped by the organization balance behind it,
        # so that balance is funded well clear of the amounts under test.
        self.customer_credit = self.fixture.customer_credit
        self.customer_credit.value = 100000
        self.customer_credit.save(update_fields=["value"])
        # A floor above each month's usage, so every month draws twice: once for
        # what was used, once to reach the minimum. The second draw writes no
        # invoice item — it is the half of the history only the audit trail sees.
        self.usage = 300
        self.floor = 900
        self.credit = factories.ProjectCreditFactory(
            project=self.project,
            value=10000,
            expected_consumption=self.floor,
            grace_coefficient=0,
        )

    # --- billing -----------------------------------------------------------

    def bill(self, year, month):
        """Bill one month, at the time its finalization would have run.

        Real finalization bills a month from the month after it, which is the
        assumption the command's inferred billing period rests on.
        """
        invoice = factories.InvoiceFactory(
            customer=self.customer, year=year, month=month
        )
        factories.InvoiceItemFactory(
            name=f"OFFERING-{month:02}",
            resource=self.fixture.resource,
            project=self.project,
            invoice=invoice,
            unit_price=10,
            quantity=self.usage / 10,
        )
        finalized_on = f"{year + month // 12}-{month % 12 + 1:02}-01"
        with freeze_time(finalized_on):
            compensations.MonthlyCompensation(
                self.customer, invoice=invoice
            ).apply_compensations()
        return invoice

    # --- standing in for a pre-ledger history ------------------------------

    def forget(self, credit=None, keep_grant=False):
        rows = (credit or self.credit).transactions.all()
        if keep_grant:
            rows = rows.exclude(transaction_type=TYPES.STAFF_GRANT)
        rows.delete()

    def destroy_the_evidence(self):
        """No invoice items, no audit events: a balance that just moved."""
        models.InvoiceItem.objects.filter(credit__isnull=False).delete()
        logging_models.Event.objects.all().delete()

    # --- running the command -----------------------------------------------

    def backfill(self, *args, project=True):
        output = StringIO()
        scope = (
            ["--project", self.project.uuid.hex]
            if project
            else ["--customer", self.customer.uuid.hex]
        )
        call_command("backfill_credit_ledger", *scope, *args, stdout=output)
        return output.getvalue()

    # --- reading the ledger back -------------------------------------------

    def rows(self, credit=None):
        return (credit or self.credit).transactions.all()

    def total(self, transaction_type, credit=None):
        return sum(
            row.amount
            for row in self.rows(credit)
            if row.transaction_type == transaction_type
        )

    def totals(self, credit=None):
        return {
            transaction_type: self.total(transaction_type, credit)
            for transaction_type, _ in TYPES.CHOICES
            if self.total(transaction_type, credit)
        }

    def months(self, transaction_type, credit=None):
        return sorted(
            str(row.billing_period)
            for row in self.rows(credit)
            if row.transaction_type == transaction_type
        )

    # --- scenarios ---------------------------------------------------------

    def test_a_forgotten_month_comes_back_from_its_evidence(self):
        self.bill(2024, 1)
        recorded = self.totals()
        self.forget()

        self.backfill()

        # Both halves of the month: what usage took, and the floor draw that
        # left no invoice item and would otherwise be lost for good.
        self.assertEqual(self.total(TYPES.COMPENSATION), recorded[TYPES.COMPENSATION])
        self.assertEqual(self.total(TYPES.MINIMAL_DRAW), recorded[TYPES.MINIMAL_DRAW])

    def test_a_forgotten_history_reconciles_to_the_current_balance(self):
        for month in (1, 2, 3):
            self.bill(2024, month)
        self.forget()

        self.backfill()

        self.credit.refresh_from_db()
        # The property the dashboard reads: granted = used + lost + remaining.
        # The grant is unrecoverable, so it is inferred from the rest.
        self.assertEqual(sum(row.amount for row in self.rows()), self.credit.value)
        self.assertEqual(
            self.rows().filter(transaction_type=TYPES.ADJUSTMENT).count(), 1
        )

    def test_each_month_is_reconstructed_under_its_own_billing_period(self):
        for month in (1, 2, 3):
            self.bill(2024, month)
        self.forget()

        self.backfill()

        self.assertEqual(
            self.months(TYPES.COMPENSATION),
            ["2024-01-01", "2024-02-01", "2024-03-01"],
        )
        # Floor draws have no billing period of their own; theirs is inferred
        # from when the finalization that made them ran, one month later.
        self.assertEqual(
            self.months(TYPES.MINIMAL_DRAW),
            ["2024-01-01", "2024-02-01", "2024-03-01"],
        )

    def test_months_the_ledger_already_records_are_left_alone(self):
        # The common case: the ledger was deployed mid-life, so recent months
        # are recorded and older ones are not. Reconstructing a recorded month
        # would double its drawdown.
        self.bill(2024, 1)
        before = self.totals()

        output = self.backfill()

        self.assertEqual(self.totals(), before)
        self.assertIn("already in the ledger", output)

    def test_only_the_gap_before_the_ledger_is_filled(self):
        self.bill(2024, 1)
        self.bill(2024, 2)
        complete = self.totals()
        # The ledger was switched on between the two months: the first is a
        # memory, the second a record.
        self.rows().filter(billing_period__year=2024, billing_period__month=1).delete()

        self.backfill()

        self.assertEqual(self.months(TYPES.COMPENSATION), ["2024-01-01", "2024-02-01"])
        self.assertEqual(self.months(TYPES.MINIMAL_DRAW), ["2024-01-01", "2024-02-01"])
        # January is back at the amounts it had, February was not touched, and
        # no balancing row was needed — so the two months are counted exactly
        # once between them.
        self.assertEqual(self.totals(), complete)

    def test_the_organization_balance_is_reconstructed_too(self):
        self.bill(2024, 1)
        recorded = self.totals(self.customer_credit)
        self.forget(self.customer_credit)

        self.backfill(project=False)

        self.customer_credit.refresh_from_db()
        self.assertEqual(
            self.total(TYPES.COMPENSATION, self.customer_credit),
            recorded[TYPES.COMPENSATION],
        )
        self.assertEqual(
            sum(row.amount for row in self.rows(self.customer_credit)),
            self.customer_credit.value,
        )

    def test_the_project_filter_leaves_the_organization_balance_alone(self):
        self.bill(2024, 1)
        self.forget()
        self.forget(self.customer_credit)

        self.backfill()

        self.assertTrue(self.rows().exists())
        self.assertFalse(self.rows(self.customer_credit).exists())

    def test_months_outside_the_window_are_not_reconstructed(self):
        for month in (1, 2, 3):
            self.bill(2024, month)
        self.forget()

        self.backfill("--since", "2024-02", "--until", "2024-02")

        self.assertEqual(self.months(TYPES.COMPENSATION), ["2024-02-01"])
        self.assertEqual(self.months(TYPES.MINIMAL_DRAW), ["2024-02-01"])

    def test_an_uncertain_month_can_be_left_empty_instead_of_guessed(self):
        self.bill(2024, 1)
        forgotten = self.totals()
        self.forget()

        self.backfill("--infer-period=none")

        # The amount is evidence; the month is not. An operator who knows the
        # finalization dates are unreliable can keep the first without the
        # second, and the totals still reconcile.
        self.assertEqual(self.months(TYPES.MINIMAL_DRAW), ["None"])
        self.assertEqual(self.total(TYPES.MINIMAL_DRAW), forgotten[TYPES.MINIMAL_DRAW])

    def test_a_balance_with_no_evidence_at_all_gets_one_opening_row(self):
        self.forget()

        self.backfill()

        self.credit.refresh_from_db()
        self.assertEqual(
            [(row.transaction_type, row.amount) for row in self.rows()],
            [(TYPES.ADJUSTMENT, self.credit.value)],
        )

    def test_drawdown_with_no_surviving_trace_is_named_as_such(self):
        self.bill(2024, 1)
        # The grant is recorded, the drawdown is not, and nothing explains the
        # difference — a balance that fell with nothing to show for it.
        self.forget(keep_grant=True)
        self.destroy_the_evidence()

        output = self.backfill()

        self.credit.refresh_from_db()
        unexplained = self.rows().get(transaction_type=TYPES.ADJUSTMENT)
        self.assertLess(unexplained.amount, 0)
        self.assertIn("no surviving trace", unexplained.comment)
        self.assertIn("nothing to show for it", output)
        self.assertEqual(sum(row.amount for row in self.rows()), self.credit.value)

    def test_the_gap_stays_visible_when_the_balancing_row_is_declined(self):
        self.bill(2024, 1)
        self.forget()

        self.backfill("--no-opening-balance")

        self.credit.refresh_from_db()
        self.assertFalse(self.rows().filter(transaction_type=TYPES.ADJUSTMENT).exists())
        # Only reconstructed drawdown, which cannot reach the current value on
        # its own: the flag's point is to leave that gap where it can be seen.
        self.assertLess(sum(row.amount for row in self.rows()), self.credit.value)

    def test_dry_run_writes_nothing(self):
        self.bill(2024, 1)
        self.forget()

        output = self.backfill("--dry-run")

        self.assertFalse(self.rows().exists())
        self.assertIn("Dry run", output)

    def test_a_rerun_takes_account_of_billing_that_happened_since(self):
        # Backfilled once, then left running: the months after the backfill are
        # recorded normally. A second run must replace its own reconstruction
        # without touching, or duplicating, what the ledger recorded meanwhile.
        self.bill(2024, 1)
        self.forget()
        self.backfill()
        self.bill(2024, 2)

        self.backfill("--force")

        self.credit.refresh_from_db()
        self.assertEqual(self.months(TYPES.COMPENSATION), ["2024-01-01", "2024-02-01"])
        self.assertEqual(self.months(TYPES.MINIMAL_DRAW), ["2024-01-01", "2024-02-01"])
        self.assertEqual(
            self.rows().filter(transaction_type=TYPES.ADJUSTMENT).count(), 1
        )
        self.assertEqual(sum(row.amount for row in self.rows()), self.credit.value)

    def test_a_second_run_neither_repeats_nor_duplicates_the_first(self):
        self.bill(2024, 1)
        self.forget()
        self.backfill()
        first = self.totals()

        skipped = self.backfill()
        forced = self.backfill("--force")

        self.assertIn("already backfilled", skipped)
        self.assertNotIn("already backfilled", forced)
        self.assertEqual(self.totals(), first)
