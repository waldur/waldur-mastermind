import datetime
from unittest import mock

from django.test import TestCase, override_settings
from freezegun import freeze_time

from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.invoices.tests import factories

GRACE_PERIOD_SETTINGS = {
    "ISSUER_DETAILS": {},
    "PAYMENT_INTERVAL": 30,
    "INVOICE_REPORTING": {"ENABLE": False, "EMAIL": "", "CSV_PARAMS": {}},
    "SEND_CUSTOMER_INVOICES": False,
    "INVOICE_FINALIZATION_GRACE_PERIOD_HOURS": 24,
}

NO_GRACE_PERIOD_SETTINGS = {
    "ISSUER_DETAILS": {},
    "PAYMENT_INTERVAL": 30,
    "INVOICE_REPORTING": {"ENABLE": False, "EMAIL": "", "CSV_PARAMS": {}},
    "SEND_CUSTOMER_INVOICES": False,
    "INVOICE_FINALIZATION_GRACE_PERIOD_HOURS": 0,
}


class StateTransitionTest(TestCase):
    """Test state transition guards for the new PENDING_FINALIZATION state."""

    def test_set_pending_finalization_from_pending(self):
        invoice = factories.InvoiceFactory(state=models.Invoice.States.PENDING)
        invoice.set_pending_finalization()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING_FINALIZATION)

    def test_set_pending_finalization_from_created_raises(self):
        invoice = factories.InvoiceFactory(state=models.Invoice.States.CREATED)
        with self.assertRaises(IncorrectStateException):
            invoice.set_pending_finalization()

    def test_set_pending_finalization_from_paid_raises(self):
        invoice = factories.InvoiceFactory(state=models.Invoice.States.PAID)
        with self.assertRaises(IncorrectStateException):
            invoice.set_pending_finalization()

    def test_set_created_from_pending(self):
        invoice = factories.InvoiceFactory(state=models.Invoice.States.PENDING)
        invoice.set_created()
        self.assertIn(
            invoice.state,
            [models.Invoice.States.CREATED, models.Invoice.States.PAID],
        )

    def test_set_created_from_pending_finalization(self):
        invoice = factories.InvoiceFactory(
            state=models.Invoice.States.PENDING_FINALIZATION
        )
        invoice.set_created()
        self.assertIn(
            invoice.state,
            [models.Invoice.States.CREATED, models.Invoice.States.PAID],
        )

    def test_set_created_from_created_raises(self):
        invoice = factories.InvoiceFactory(state=models.Invoice.States.CREATED)
        with self.assertRaises(IncorrectStateException):
            invoice.set_created()

    def test_set_created_from_canceled_raises(self):
        invoice = factories.InvoiceFactory(state=models.Invoice.States.CANCELED)
        with self.assertRaises(IncorrectStateException):
            invoice.set_created()


class MutableStatesTest(TestCase):
    """Verify MUTABLE_STATES constant contains the right states."""

    def test_mutable_states_includes_pending(self):
        self.assertIn(
            models.Invoice.States.PENDING, models.Invoice.States.MUTABLE_STATES
        )

    def test_mutable_states_includes_pending_finalization(self):
        self.assertIn(
            models.Invoice.States.PENDING_FINALIZATION,
            models.Invoice.States.MUTABLE_STATES,
        )

    def test_mutable_states_excludes_created(self):
        self.assertNotIn(
            models.Invoice.States.CREATED, models.Invoice.States.MUTABLE_STATES
        )

    def test_mutable_states_excludes_paid(self):
        self.assertNotIn(
            models.Invoice.States.PAID, models.Invoice.States.MUTABLE_STATES
        )


@override_settings(WALDUR_INVOICES=NO_GRACE_PERIOD_SETTINGS)
class BackwardCompatibilityTest(TestCase):
    """When grace_period=0, behavior should be identical to original."""

    def test_old_invoices_finalized_immediately(self):
        with freeze_time("2017-01-15"):
            invoice = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)

    def test_no_pending_finalization_state_used(self):
        with freeze_time("2017-01-15"):
            factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        self.assertFalse(
            models.Invoice.objects.filter(
                state=models.Invoice.States.PENDING_FINALIZATION
            ).exists()
        )


@override_settings(WALDUR_INVOICES=GRACE_PERIOD_SETTINGS)
class GracePeriodFlowTest(TestCase):
    """Test the full grace period lifecycle."""

    def test_create_monthly_invoices_transitions_to_pending_finalization(self):
        """On the 1st, old invoices should transition to PENDING_FINALIZATION."""
        with freeze_time("2017-01-15"):
            invoice = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING_FINALIZATION)

    def test_finalize_previous_invoices_transitions_to_created(self):
        """After grace period, PENDING_FINALIZATION invoices should become CREATED."""
        with freeze_time("2017-01-15"):
            invoice = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING_FINALIZATION)

        with freeze_time("2017-02-02"):
            tasks.finalize_previous_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)

    def test_new_month_invoice_created_during_grace_period(self):
        """New month invoices should be created even during grace period."""
        fixture = structure_fixtures.CustomerFixture()

        with freeze_time("2017-01-15"):
            factories.InvoiceFactory(customer=fixture.customer, year=2017, month=1)

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        # New February invoice should exist
        self.assertTrue(
            models.Invoice.objects.filter(
                customer=fixture.customer, year=2017, month=2
            ).exists()
        )

    def test_only_one_pending_invoice_exists(self):
        """During grace period, only one invoice should be PENDING (current month)."""
        fixture = structure_fixtures.CustomerFixture()

        with freeze_time("2017-01-15"):
            factories.InvoiceFactory(customer=fixture.customer, year=2017, month=1)

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        pending_invoices = models.Invoice.objects.filter(
            customer=fixture.customer, state=models.Invoice.States.PENDING
        )
        self.assertEqual(pending_invoices.count(), 1)
        self.assertEqual(pending_invoices.first().month, 2)

    def test_finalize_is_idempotent(self):
        """Calling finalize_previous_invoices twice should be safe."""
        with freeze_time("2017-01-15"):
            invoice = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        with freeze_time("2017-02-02"):
            tasks.finalize_previous_invoices()
            # Second call should not raise
            tasks.finalize_previous_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)

    def test_finalize_skipped_when_grace_period_not_elapsed(self):
        """Finalization should not happen if grace period hasn't elapsed yet."""
        with freeze_time("2017-01-15"):
            invoice = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING_FINALIZATION)

        # Only 12 hours later, grace period is 24h — should NOT finalize
        with freeze_time("2017-02-01 12:00:00"):
            tasks.finalize_previous_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING_FINALIZATION)

        # 25 hours later — should finalize
        with freeze_time("2017-02-02 01:00:00"):
            tasks.finalize_previous_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)

    def test_multiple_old_invoices_transition_to_pending_finalization(self):
        """Multiple old PENDING invoices should all transition."""
        with freeze_time("2016-11-01"):
            invoice1 = factories.InvoiceFactory()

        with freeze_time("2017-01-15"):
            invoice2 = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice1.refresh_from_db()
        invoice2.refresh_from_db()
        self.assertEqual(invoice1.state, models.Invoice.States.PENDING_FINALIZATION)
        self.assertEqual(invoice2.state, models.Invoice.States.PENDING_FINALIZATION)


@override_settings(WALDUR_INVOICES=GRACE_PERIOD_SETTINGS)
class ReportTimingTest(TestCase):
    """Reports should only be sent after finalization, not during grace period."""

    @override_settings(
        WALDUR_INVOICES={
            **GRACE_PERIOD_SETTINGS,
            "INVOICE_REPORTING": {
                "ENABLE": True,
                "EMAIL": "test@example.com",
                "CSV_PARAMS": {"delimiter": ";"},
            },
        }
    )
    def test_reports_not_sent_during_grace_period(self):
        """When grace period is active, reports are not sent on the 1st."""
        with freeze_time("2017-01-15"):
            factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            with mock.patch.object(tasks.send_invoice_report, "delay") as mock_report:
                tasks.create_monthly_invoices()
                mock_report.assert_not_called()

    @override_settings(
        WALDUR_INVOICES={
            **GRACE_PERIOD_SETTINGS,
            "INVOICE_REPORTING": {
                "ENABLE": True,
                "EMAIL": "test@example.com",
                "CSV_PARAMS": {"delimiter": ";"},
            },
        }
    )
    def test_reports_sent_after_finalization(self):
        """Reports are sent when finalize_previous_invoices runs."""
        with freeze_time("2017-01-15"):
            factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        with freeze_time("2017-02-02"):
            with mock.patch.object(tasks.send_invoice_report, "delay") as mock_report:
                tasks.finalize_previous_invoices()
                mock_report.assert_called_once()

    @override_settings(
        WALDUR_INVOICES={
            **GRACE_PERIOD_SETTINGS,
            "INVOICE_REPORTING": {
                "ENABLE": True,
                "EMAIL": "test@example.com",
                "CSV_PARAMS": {"delimiter": ";"},
            },
            "SEND_CUSTOMER_INVOICES": True,
        }
    )
    def test_reports_not_sent_when_some_invoices_fail(self):
        """If some invoices fail to finalize, reports should NOT be sent."""
        with freeze_time("2017-01-15"):
            invoice1 = factories.InvoiceFactory()
            invoice2 = factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice1.refresh_from_db()
        invoice2.refresh_from_db()
        self.assertEqual(invoice1.state, models.Invoice.States.PENDING_FINALIZATION)
        self.assertEqual(invoice2.state, models.Invoice.States.PENDING_FINALIZATION)

        call_count = [0]
        original_process = tasks.process_invoice_credits

        def fail_on_second_call(invoice):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Simulated failure")
            return original_process(invoice)

        with freeze_time("2017-02-02"):
            with (
                mock.patch.object(tasks.send_invoice_report, "delay") as mock_report,
                mock.patch.object(
                    tasks.send_new_invoices_notification, "delay"
                ) as mock_notify,
                mock.patch(
                    "waldur_mastermind.invoices.tasks.process_invoice_credits",
                    side_effect=fail_on_second_call,
                ),
            ):
                tasks.finalize_previous_invoices()
                # One invoice failed, so reports should NOT be sent
                mock_report.assert_not_called()
                mock_notify.assert_not_called()

    @override_settings(
        WALDUR_INVOICES={
            **GRACE_PERIOD_SETTINGS,
            "INVOICE_REPORTING": {
                "ENABLE": True,
                "EMAIL": "test@example.com",
                "CSV_PARAMS": {"delimiter": ";"},
            },
            "SEND_CUSTOMER_INVOICES": True,
        }
    )
    def test_reports_sent_when_remaining_invoices_finalized(self):
        """Reports should be sent when the last failed invoice is finalized."""
        with freeze_time("2017-01-15"):
            factories.InvoiceFactory()
            factories.InvoiceFactory()

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        call_count = [0]
        original_process = tasks.process_invoice_credits

        def fail_on_second_call(invoice):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Simulated failure")
            return original_process(invoice)

        # First run: one succeeds, one fails
        with freeze_time("2017-02-02"):
            with mock.patch(
                "waldur_mastermind.invoices.tasks.process_invoice_credits",
                side_effect=fail_on_second_call,
            ):
                tasks.finalize_previous_invoices()

        # Second run: remaining invoice succeeds, reports should be sent
        with freeze_time("2017-02-02 01:00:00"):
            with (
                mock.patch.object(tasks.send_invoice_report, "delay") as mock_report,
                mock.patch.object(
                    tasks.send_new_invoices_notification, "delay"
                ) as mock_notify,
            ):
                tasks.finalize_previous_invoices()
                mock_report.assert_called_once()
                mock_notify.assert_called_once()


@override_settings(WALDUR_INVOICES=GRACE_PERIOD_SETTINGS)
class CreditEndDateWithGracePeriodTest(TestCase):
    """Credits with end_date on the 1st must not be zeroed before compensation."""

    def test_credit_with_end_date_on_first_not_zeroed_before_compensation(self):
        """
        When grace_hours=24, finalization runs on Feb 2. A credit with
        end_date=Feb 1 should NOT be zeroed by set_to_zero_overdue_credits,
        so that process_invoice_credits can still apply the compensation.
        """
        with freeze_time("2017-01-15"):
            credit = factories.CustomerCreditFactory(
                value=100,
                end_date=datetime.date(2017, 2, 1),
            )
            invoice = factories.InvoiceFactory(
                customer=credit.customer, year=2017, month=1
            )

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, models.Invoice.States.PENDING_FINALIZATION)

        # Finalize on Feb 2 (after grace period).
        # The key assertion: process_invoice_credits is called with credit.value > 0.
        credit_value_when_processing = []
        original_process = tasks.process_invoice_credits

        def capture_credit_value(inv):
            credit.refresh_from_db()
            credit_value_when_processing.append(float(credit.value))
            return original_process(inv)

        with freeze_time("2017-02-02"):
            with mock.patch(
                "waldur_mastermind.invoices.tasks.process_invoice_credits",
                side_effect=capture_credit_value,
            ):
                tasks.finalize_previous_invoices()

        # Credit was NOT zero when process_invoice_credits was called
        self.assertEqual(len(credit_value_when_processing), 1)
        self.assertGreater(
            credit_value_when_processing[0],
            0,
            "Credit should not be zeroed before process_invoice_credits",
        )

    def test_credit_with_end_date_before_first_is_zeroed(self):
        """Credits with end_date before the 1st should be zeroed before compensation."""
        with freeze_time("2017-01-15"):
            credit = factories.CustomerCreditFactory(
                value=100,
                end_date=datetime.date(2017, 1, 1),
            )
            factories.InvoiceFactory(customer=credit.customer, year=2017, month=1)

        with freeze_time("2017-02-01"):
            tasks.create_monthly_invoices()

        credit_value_when_processing = []
        original_process = tasks.process_invoice_credits

        def capture_credit_value(inv):
            credit.refresh_from_db()
            credit_value_when_processing.append(float(credit.value))
            return original_process(inv)

        with freeze_time("2017-02-02"):
            with mock.patch(
                "waldur_mastermind.invoices.tasks.process_invoice_credits",
                side_effect=capture_credit_value,
            ):
                tasks.finalize_previous_invoices()

        # Credit WAS zero when process_invoice_credits was called
        self.assertEqual(len(credit_value_when_processing), 1)
        self.assertEqual(
            credit_value_when_processing[0],
            0,
            "Credit with end_date before the 1st should be zeroed before compensation",
        )

    def test_set_to_zero_overdue_credits_with_effective_date(self):
        """set_to_zero_overdue_credits respects the effective_date parameter."""
        with freeze_time("2017-02-02"):
            credit_before = factories.CustomerCreditFactory(
                value=100,
                end_date=datetime.date(2017, 1, 1),
            )
            credit_on_first = factories.CustomerCreditFactory(
                value=100,
                end_date=datetime.date(2017, 2, 1),
            )
            credit_after = factories.CustomerCreditFactory(
                value=100,
                end_date=datetime.date(2017, 3, 1),
            )

            # Effective date is Feb 1, so only credits with end_date < Feb 1 are zeroed
            tasks.set_to_zero_overdue_credits(effective_date=datetime.date(2017, 2, 1))

            credit_before.refresh_from_db()
            credit_on_first.refresh_from_db()
            credit_after.refresh_from_db()

            self.assertEqual(credit_before.value, 0)
            self.assertEqual(credit_on_first.value, 100)  # NOT zeroed
            self.assertEqual(credit_after.value, 100)
