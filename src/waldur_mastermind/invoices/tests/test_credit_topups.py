"""Idempotent credit top-ups and the channel they arrived through."""

import contextlib
import threading
import time
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models, topups
from waldur_mastermind.invoices.tests import factories
from waldur_mastermind.invoices.tests.test_credit_lots import BaseLotTest, balances

Kinds = models.CreditLot.Kinds
Channels = models.CreditTransaction.Channels


class IdempotentTopUpTest(BaseLotTest):
    def top_up_rows(self, credit):
        return credit.transactions.filter(
            transaction_type=models.CreditTransaction.Types.TOP_UP
        )

    def test_repeated_reference_with_same_amount_credits_once(self):
        credit = self.create_credit()
        first = self.top_up(credit, "250", external_reference="ERP-1")
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        second = self.top_up(credit, "250.00", external_reference="ERP-1")
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.data)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("250"))
        self.assertEqual(balances(credit)[Kinds.PAID], Decimal("250"))
        self.assertEqual(Decimal(second.data["paid_balance"]), Decimal("250"))
        self.assertEqual(self.top_up_rows(credit).count(), 1)
        self.assertEqual(
            Event.objects.filter(event_type=EventType.CREDIT_TOPPED_UP).count(), 1
        )

    def test_retry_keeps_the_first_comment(self):
        credit = self.create_credit()
        self.top_up(credit, "250", external_reference="ERP-1")
        response = self.top_up(
            credit,
            "250",
            comment="Resent",
            payment_reference="BANK-9",
            external_reference="ERP-1",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.top_up_rows(credit).get().comment, "Bank transfer")

    def test_repeated_reference_with_another_amount_is_refused(self):
        credit = self.create_credit()
        self.top_up(credit, "250", external_reference="ERP-1")
        response = self.top_up(credit, "300", external_reference="ERP-1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("external_reference", response.data)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("250"))
        self.assertEqual(self.top_up_rows(credit).count(), 1)

    def test_same_reference_on_another_organization_is_accepted(self):
        credit = self.create_credit()
        other_credit = factories.CustomerCreditFactory(
            customer=structure_factories.CustomerFactory(), value=Decimal("0")
        )
        self.top_up(credit, "250", external_reference="ERP-1")
        response = self.top_up(other_credit, "250", external_reference="ERP-1")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        other_credit.refresh_from_db()
        self.assertEqual(other_credit.value, Decimal("250"))
        self.assertEqual(self.top_up_rows(other_credit).count(), 1)

    def test_top_ups_without_reference_are_never_deduplicated(self):
        credit = self.create_credit()
        self.top_up(credit, "100")
        self.top_up(credit, "100")
        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("200"))
        self.assertEqual(self.top_up_rows(credit).count(), 2)

    def test_reversal_with_reference_is_idempotent(self):
        credit = self.create_credit()
        self.top_up(credit, "100", external_reference="ERP-1")
        self.top_up(credit, "100", external_reference="ERP-2")
        for _ in range(2):
            response = self.top_up(
                credit, "-100", comment="Bounced", external_reference="ERP-2-bounce"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("100"))
        self.assertEqual(balances(credit)[Kinds.PAID], Decimal("100"))

    def test_retry_is_answered_before_the_reversal_limit_is_checked(self):
        # The reversal emptied the paid credit, so applying it again would be
        # refused; a retry of it must still be recognised as already done.
        credit = self.create_credit()
        self.top_up(credit, "100", external_reference="ERP-1")
        self.top_up(credit, "-100", external_reference="ERP-1-bounce")
        response = self.top_up(credit, "-100", external_reference="ERP-1-bounce")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("0"))

    def test_channel_follows_the_external_reference(self):
        credit = self.create_credit()
        self.top_up(credit, "100", external_reference="ERP-1")
        self.top_up(credit, "50", payment_reference="BANK-7")

        api_row = self.top_up_rows(credit).get(amount=Decimal("100"))
        manual_row = self.top_up_rows(credit).get(amount=Decimal("50"))
        self.assertEqual(api_row.channel, Channels.API)
        self.assertEqual(api_row.external_reference, "ERP-1")
        self.assertEqual(manual_row.channel, Channels.MANUAL)
        self.assertEqual(manual_row.external_reference, "")
        self.assertIn("BANK-7", manual_row.comment)

    def test_event_names_the_channel_and_reference(self):
        credit = self.create_credit()
        self.top_up(credit, "100", external_reference="ERP-1")
        event = Event.objects.filter(event_type=EventType.CREDIT_TOPPED_UP).get()
        self.assertEqual(event.context["channel"], Channels.API)
        self.assertEqual(event.context["external_reference"], "ERP-1")

    def test_other_ledger_rows_read_manual(self):
        credit = self.create_credit("100")
        row = credit.transactions.get(
            transaction_type=models.CreditTransaction.Types.STAFF_GRANT
        )
        self.assertEqual(row.channel, Channels.MANUAL)
        self.assertEqual(row.external_reference, "")

    def test_gateway_channel_is_recorded(self):
        credit = self.create_credit()
        topups.top_up(
            credit,
            Decimal("80"),
            "Card payment",
            channel=Channels.GATEWAY,
            external_reference="pi_123",
        )
        row = self.top_up_rows(credit).get()
        self.assertEqual(row.channel, Channels.GATEWAY)
        self.assertEqual(row.external_reference, "pi_123")

    @contextlib.contextmanager
    def lookup_misses_once(self):
        # Stands in for a writer that skips the credit's lock and records the
        # reference after the lookup but before the insert.
        lookup = topups._is_retry
        calls = []

        def miss_first(*args):
            calls.append(args)
            if len(calls) == 1:
                return False
            return lookup(*args)

        with mock.patch.object(topups, "_is_retry", side_effect=miss_first):
            yield

    def test_reference_recorded_past_the_lookup_is_answered_as_a_retry(self):
        credit = self.create_credit()
        self.top_up(credit, "250", external_reference="ERP-1")
        with self.lookup_misses_once():
            response = self.top_up(credit, "250", external_reference="ERP-1")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(Decimal(response.data["value"]), Decimal("250"))

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("250"))
        self.assertEqual(self.top_up_rows(credit).count(), 1)
        self.assertEqual(
            Event.objects.filter(event_type=EventType.CREDIT_TOPPED_UP).count(), 1
        )

    def test_reference_recorded_past_the_lookup_with_another_amount_is_refused(
        self,
    ):
        credit = self.create_credit()
        self.top_up(credit, "250", external_reference="ERP-1")
        with self.lookup_misses_once():
            response = self.top_up(credit, "300", external_reference="ERP-1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("external_reference", response.data)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("250"))
        self.assertEqual(self.top_up_rows(credit).count(), 1)

    def test_database_refuses_a_duplicate_reference_on_one_credit(self):
        credit = self.create_credit()
        self.top_up(credit, "100", external_reference="ERP-1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.CreditTransaction.objects.create(
                credit=credit,
                amount=Decimal("100"),
                transaction_type=models.CreditTransaction.Types.TOP_UP,
                external_reference="ERP-1",
            )


class CreditTransactionChannelFilterTest(BaseLotTest):
    def setUp(self):
        super().setUp()
        self.credit = self.create_credit("10")
        self.top_up(self.credit, "100", external_reference="ERP-1")
        self.top_up(self.credit, "50")
        self.list_url = reverse("credit-transaction-list")
        self.client.force_authenticate(self.owner)

    def list_rows(self, **params):
        response = self.client.get(
            self.list_url, {"credit_uuid": self.credit.uuid.hex, **params}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_response_includes_channel_and_external_reference(self):
        rows = {Decimal(row["amount"]): row for row in self.list_rows()}
        self.assertEqual(rows[Decimal("100")]["channel"], Channels.API)
        self.assertEqual(rows[Decimal("100")]["channel_display"], "API")
        self.assertEqual(rows[Decimal("100")]["external_reference"], "ERP-1")
        self.assertEqual(rows[Decimal("50")]["channel"], Channels.MANUAL)
        self.assertEqual(rows[Decimal("50")]["channel_display"], "Manual")
        self.assertEqual(rows[Decimal("50")]["external_reference"], "")

    def test_filter_by_channel(self):
        rows = self.list_rows(channel=Channels.API)
        self.assertEqual([Decimal(row["amount"]) for row in rows], [Decimal("100")])
        # The staff grant and the plain top-up.
        self.assertEqual(len(self.list_rows(channel=Channels.MANUAL)), 2)

    def test_filter_by_several_channels(self):
        topups.top_up(
            self.credit,
            Decimal("80"),
            "Card payment",
            channel=Channels.GATEWAY,
            external_reference="pi_123",
        )
        rows = self.list_rows(channel=[Channels.API, Channels.GATEWAY])
        self.assertEqual(
            sorted(Decimal(row["amount"]) for row in rows),
            [Decimal("80"), Decimal("100")],
        )

    def test_filter_by_external_reference(self):
        rows = self.list_rows(external_reference="ERP-1")
        self.assertEqual([Decimal(row["amount"]) for row in rows], [Decimal("100")])
        self.assertEqual(self.list_rows(external_reference="ERP-2"), [])


class ConcurrentTopUpTest(test.APITransactionTestCase):
    """Two retries arriving together must credit once.

    APITransactionTestCase required: each thread commits on its own database
    connection, so the calls can only race outside a wrapping test transaction.
    """

    def wait_for_lock_waiters(self, count):
        deadline = time.monotonic() + 10
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                if cursor.fetchone()[0] >= count:
                    return
                time.sleep(0.05)
        self.fail(f"{count} calls never queued on the credit lock.")

    def test_concurrent_calls_with_the_same_reference_credit_once(self):
        credit = factories.CustomerCreditFactory(
            customer=structure_factories.CustomerFactory(), value=Decimal("0")
        )
        errors = []

        def call():
            try:
                topups.top_up(
                    credit,
                    Decimal("250"),
                    "Bank transfer",
                    channel=Channels.API,
                    external_reference="ERP-1",
                )
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=call) for _ in range(2)]
        try:
            # Hold the credit's lock until both calls wait on it, so neither can
            # have looked for the other's row before taking the lock.
            with transaction.atomic():
                models.CustomerCredit.objects.select_for_update().get(pk=credit.pk)
                for thread in threads:
                    thread.start()
                self.wait_for_lock_waiters(2)
        finally:
            # Even when the wait fails, so no call outlives the test's teardown.
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=30)

        self.assertFalse(
            any(thread.is_alive() for thread in threads), "A call never finished."
        )
        self.assertEqual(errors, [])
        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("250"))
        self.assertEqual(
            credit.transactions.filter(external_reference="ERP-1").count(), 1
        )
