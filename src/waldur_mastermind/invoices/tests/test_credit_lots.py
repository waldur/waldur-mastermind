"""Credit lots: the paid, granted and earned parts of an organization credit."""

import datetime
import importlib
from decimal import Decimal

from ddt import data, ddt, unpack
from django.apps import apps
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import compensations, ledger, lots, models, tasks
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

Kinds = models.CreditLot.Kinds


def balances(credit):
    """Remaining amount per lot kind, with every kind present."""
    result = {kind: Decimal("0") for kind, _ in Kinds.CHOICES}
    for lot in models.CreditLot.objects.filter(credit=credit):
        result[lot.kind] += lot.remaining
    return result


class BaseLotTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.customer = self.fixture.customer
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

    def create_credit(self, value="0"):
        return factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal(value)
        )

    def top_up(self, credit, amount, comment="Bank transfer", user=None, **extra):
        self.client.force_authenticate(user or self.staff)
        return self.client.post(
            factories.CustomerCreditFactory.get_url(credit, action="top_up"),
            {"amount": amount, "comment": comment, **extra},
        )

    def reclassify(self, credit, amount, comment="Was a prepayment", user=None):
        self.client.force_authenticate(user or self.staff)
        return self.client.post(
            factories.CustomerCreditFactory.get_url(credit, action="reclassify"),
            {"amount": amount, "comment": comment},
        )

    def adjust_withdrawable(self, credit, amount, comment="manual"):
        self.client.force_authenticate(self.staff)
        return self.client.post(
            factories.CustomerCreditFactory.get_url(
                credit, action="adjust_withdrawable"
            ),
            {"amount": amount, "comment": comment},
        )

    def compensate(self):
        # 10 x 30 = 300 of chargeable usage on the open invoice.
        self.fixture.invoice_item
        compensations.MonthlyCompensation(
            self.customer, invoice=self.fixture.invoice
        ).apply_compensations()

    def assert_lots_match_value(self, credit):
        credit.refresh_from_db()
        self.assertEqual(sum(balances(credit).values()), credit.value)


class TopUpTest(BaseLotTest):
    def test_staff_top_up_creates_paid_lot_and_ledger_row(self):
        credit = self.create_credit()
        response = self.top_up(credit, "250", payment_reference="INV-2026-17")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("250"))
        self.assertEqual(balances(credit)[Kinds.PAID], Decimal("250"))
        row = credit.transactions.get(
            transaction_type=models.CreditTransaction.Types.TOP_UP
        )
        self.assertEqual(row.amount, Decimal("250"))
        self.assertIn("Bank transfer", row.comment)
        self.assertIn("INV-2026-17", row.comment)

    def test_response_reports_balances_by_kind(self):
        credit = self.create_credit("100")
        response = self.top_up(credit, "250")
        self.assertEqual(Decimal(response.data["paid_balance"]), Decimal("250"))
        self.assertEqual(Decimal(response.data["granted_balance"]), Decimal("100"))
        self.assertEqual(Decimal(response.data["withdrawable_balance"]), Decimal("0"))

    def test_staff_edit_cannot_lower_paid_credit(self):
        credit = self.create_credit("100")
        self.top_up(credit, "50")
        url = factories.CustomerCreditFactory.get_url(credit)
        response = self.client.patch(url, {"value": "40"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(url, {"value": "50"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(Decimal(response.data["paid_balance"]), Decimal("50"))

    def test_staff_edit_of_value_reports_fresh_balances(self):
        credit = self.create_credit("100")
        self.client.force_authenticate(self.staff)
        response = self.client.patch(
            factories.CustomerCreditFactory.get_url(credit), {"value": "150"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(Decimal(response.data["granted_balance"]), Decimal("150"))

    def test_owner_cannot_top_up(self):
        credit = self.create_credit()
        response = self.top_up(credit, "250", user=self.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_amount_must_not_be_zero(self):
        credit = self.create_credit()
        response = self.top_up(credit, "0")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_negative_top_up_reverses_paid_credit_only(self):
        # A top-up of 1000 recorded instead of 100: the correction must not
        # turn granted credit into paid.
        credit = self.create_credit("100")
        self.top_up(credit, "1000")
        response = self.top_up(credit, "-900", comment="Typo in amount")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("200"))
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("100"),
                Kinds.PAID: Decimal("100"),
                Kinds.EARNED: 0,
            },
        )

    def test_reversal_cannot_exceed_the_paid_balance(self):
        credit = self.create_credit("100")
        self.top_up(credit, "50")
        response = self.top_up(credit, "-60")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("150"))

    def test_top_up_is_audited_with_its_comment_and_user(self):
        credit = self.create_credit()
        self.top_up(credit, "250", payment_reference="INV-2026-17")
        event = Event.objects.filter(event_type=EventType.CREDIT_TOPPED_UP).get()
        self.assertIn("topped up by 250", event.message)
        self.assertIn("INV-2026-17", event.message)
        self.assertEqual(event.context["user_uuid"], self.staff.uuid.hex)

    def test_comment_is_required(self):
        credit = self.create_credit()
        response = self.top_up(credit, "10", comment=" ")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_config(PAT_ENABLED=True)
class CreditTokenScopeTest(BaseLotTest):
    """A staff user's personal access token acts as staff on credits only
    with the staff scope, and only on the organizations it is bound to."""

    def setUp(self):
        super().setUp()
        self.staff.can_use_personal_access_tokens = True
        self.staff.save()
        Token.objects.get_or_create(user=self.staff)
        self.credit = self.create_credit("100")

    def post_with_token(self, action, scopes, bindings=()):
        pat = _create_pat(self.staff, scopes=scopes, bindings=list(bindings))
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        return self.client.post(
            factories.CustomerCreditFactory.get_url(self.credit, action=action),
            {"amount": "50", "comment": "Bank transfer"},
        )

    def test_token_without_staff_scope_cannot_move_credit(self):
        for action in ("top_up", "reclassify", "adjust_withdrawable"):
            with self.subTest(action):
                response = self.post_with_token(
                    action, [PermissionEnum.LIST_ORDERS.value]
                )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(balances(self.credit)[Kinds.GRANTED], Decimal("100"))

    def test_token_with_staff_scope_can_top_up(self):
        response = self.post_with_token("top_up", [PermissionEnum.STAFF_ACCESS.value])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(balances(self.credit)[Kinds.PAID], Decimal("50"))

    def test_token_without_staff_scope_cannot_pay_earlier_invoices(self):
        project_credit = factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("50")
        )
        pat = _create_pat(
            self.staff, scopes=[PermissionEnum.LIST_ORDERS.value], bindings=[]
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.post(
            factories.ProjectCreditFactory.get_url(
                project_credit, action="pay_earlier_invoices"
            ),
            {"dry_run": False},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_token_bound_to_another_organization_cannot_top_up(self):
        response = self.post_with_token(
            "top_up",
            [PermissionEnum.STAFF_ACCESS.value],
            bindings=[structure_factories.CustomerFactory()],
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(balances(self.credit)[Kinds.PAID], 0)


class LotKindsTest(BaseLotTest):
    def test_staff_grant_opens_a_granted_lot(self):
        credit = self.create_credit("100")
        self.assertEqual(balances(credit)[Kinds.GRANTED], Decimal("100"))

    def test_withdrawable_adjustment_is_earned(self):
        credit = self.create_credit()
        self.adjust_withdrawable(credit, "40")
        self.assertEqual(balances(credit)[Kinds.EARNED], Decimal("40"))

    def test_staff_reduction_draws_granted_first(self):
        credit = self.create_credit("100")
        self.top_up(credit, "50")
        credit.refresh_from_db()
        credit.value = Decimal("120")
        credit.save()
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("70"),
                Kinds.PAID: Decimal("50"),
                Kinds.EARNED: 0,
            },
        )


class CompensationDrawOrderTest(BaseLotTest):
    def test_granted_is_drawn_before_paid_before_earned(self):
        credit = self.create_credit("100")
        self.top_up(credit, "150")
        self.adjust_withdrawable(credit, "200")

        self.compensate()

        # 300 of usage: all 100 granted, all 150 paid, then 50 earned.
        self.assertEqual(
            balances(credit),
            {Kinds.GRANTED: 0, Kinds.PAID: 0, Kinds.EARNED: Decimal("150")},
        )
        self.assert_lots_match_value(credit)

    def test_ledger_reports_how_each_row_moved_each_kind(self):
        credit = self.create_credit("100")
        self.top_up(credit, "150")
        self.reclassify(credit, "40")
        self.adjust_withdrawable(credit, "200")
        self.compensate()

        self.client.force_authenticate(self.staff)
        response = self.client.get(
            reverse("credit-transaction-list"),
            {"customer_uuid": self.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        Types = models.CreditTransaction.Types

        def changes(transaction_type, sign):
            row = next(
                row
                for row in response.data
                if row["transaction_type"] == transaction_type
                and (Decimal(row["amount"]) > 0) == (sign > 0)
            )
            return tuple(
                Decimal(row[f"{kind}_change"]) for kind in ("granted", "paid", "earned")
            )

        self.assertEqual(changes(Types.TOP_UP, 1), (0, Decimal("150"), 0))
        # Reclassifying 40 is a granted draw paired with a paid inflow.
        self.assertEqual(changes(Types.RECLASSIFICATION, -1), (Decimal("-40"), 0, 0))
        self.assertEqual(changes(Types.RECLASSIFICATION, 1), (0, Decimal("40"), 0))
        self.assertEqual(
            changes(Types.WITHDRAWABLE_ADJUSTMENT, 1), (0, 0, Decimal("200"))
        )
        # 300 of usage: the 60 granted left, all 190 paid, then 50 earned.
        self.assertEqual(
            changes(Types.COMPENSATION, -1),
            (Decimal("-60"), Decimal("-190"), Decimal("-50")),
        )

    def test_oldest_lot_of_a_kind_is_drawn_first(self):
        credit = self.create_credit()
        self.top_up(credit, "200", comment="first")
        self.top_up(credit, "200", comment="second")

        self.compensate()

        first, second = models.CreditLot.objects.filter(credit=credit).order_by(
            "created", "id"
        )
        self.assertEqual(first.remaining, Decimal("0"))
        self.assertEqual(second.remaining, Decimal("100"))

    def test_reapplying_compensations_restores_and_redraws_the_same_lots(self):
        credit = self.create_credit("100")
        self.top_up(credit, "500")

        self.compensate()
        after_first = balances(credit)
        self.compensate()

        self.assertEqual(balances(credit), after_first)
        self.assertEqual(
            after_first,
            {Kinds.GRANTED: 0, Kinds.PAID: Decimal("300"), Kinds.EARNED: 0},
        )
        self.assert_lots_match_value(credit)

    def test_clearing_compensations_gives_the_lots_back(self):
        credit = self.create_credit("100")
        self.top_up(credit, "500")
        self.compensate()

        compensations.MonthlyCompensation(
            self.customer, invoice=self.fixture.invoice
        ).clear_compensations()

        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("100"),
                Kinds.PAID: Decimal("500"),
                Kinds.EARNED: 0,
            },
        )
        self.assert_lots_match_value(credit)


class ExpiryTest(BaseLotTest):
    def test_expiry_removes_only_granted_credit(self):
        with freeze_time("2024-03-15"):
            credit = self.create_credit("100")
            self.top_up(credit, "50")
            self.adjust_withdrawable(credit, "30")
            credit.refresh_from_db()
            credit.end_date = datetime.date(2024, 3, 1)
            credit.save(update_fields=["end_date"])

            tasks.set_to_zero_overdue_credits()

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("80"))
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: 0,
                Kinds.PAID: Decimal("50"),
                Kinds.EARNED: Decimal("30"),
            },
        )
        expiry = credit.transactions.get(
            transaction_type=models.CreditTransaction.Types.EXPIRY
        )
        self.assertEqual(expiry.amount, Decimal("-100"))
        # The credit is not set to zero, so the event says what did expire.
        event = Event.objects.get(event_type=EventType.GRANTED_CREDIT_EXPIRED)
        self.assertIn("Granted credit has expired", event.message)
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.SET_TO_ZERO_OVERDUE_CREDIT
            ).exists()
        )

    def test_expiry_of_a_credit_that_is_all_granted_sets_it_to_zero(self):
        with freeze_time("2024-03-15"):
            credit = self.create_credit("100")
            credit.end_date = datetime.date(2024, 3, 1)
            credit.save(update_fields=["end_date"])

            tasks.set_to_zero_overdue_credits()

        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.SET_TO_ZERO_OVERDUE_CREDIT
            ).exists()
        )
        self.assertFalse(
            Event.objects.filter(event_type=EventType.GRANTED_CREDIT_EXPIRED).exists()
        )

    def test_expired_credit_without_granted_part_is_left_alone(self):
        with freeze_time("2024-03-15"):
            credit = self.create_credit()
            self.top_up(credit, "50")
            credit.refresh_from_db()
            credit.end_date = datetime.date(2024, 3, 1)
            credit.save(update_fields=["end_date"])

            tasks.set_to_zero_overdue_credits()

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("50"))
        self.assertFalse(
            credit.transactions.filter(
                transaction_type=models.CreditTransaction.Types.EXPIRY
            ).exists()
        )


class WithdrawableBalanceTest(BaseLotTest):
    def test_payout_draws_only_earned_credit(self):
        credit = self.create_credit("100")
        self.adjust_withdrawable(credit, "40")
        self.adjust_withdrawable(credit, "-25", comment="payout")
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("100"),
                Kinds.PAID: 0,
                Kinds.EARNED: Decimal("15"),
            },
        )

    def test_grant_after_spending_earnings_is_not_withdrawable(self):
        credit = self.create_credit()
        self.adjust_withdrawable(credit, "100")
        credit.refresh_from_db()
        # The earnings pay for this month's usage...
        credit.value = Decimal("0")
        with ledger.credit_transaction_type(
            models.CreditTransaction.Types.COMPENSATION
        ):
            credit.save()
        # ...and a later grant must not become withdrawable.
        credit.value = Decimal("50")
        credit.save()

        credit.refresh_from_db()
        self.assertEqual(credit.withdrawable_balance, Decimal("0"))


class MinimalConsumptionAfterEndDateTest(BaseLotTest):
    """What survives the end date does not expire, so no floor drains it."""

    def setUp(self):
        super().setUp()
        self.credit = factories.CustomerCreditFactory(
            customer=self.customer,
            value=Decimal("0"),
            end_date=datetime.date(2026, 11, 1),
            expected_consumption=Decimal("100"),
            apply_as_minimal_consumption=True,
        )

    def test_floor_applies_until_the_end_month(self):
        with freeze_time("2026-11-01"):
            self.assertEqual(self.credit.minimal_consumption, Decimal("100"))

    def test_no_floor_after_the_end_month(self):
        with freeze_time("2026-12-01"):
            self.assertEqual(self.credit.minimal_consumption, 0)

    def test_prepayment_survives_month_close_after_end_date(self):
        self.top_up(self.credit, "500")
        with freeze_time("2026-12-01"):
            invoice = factories.InvoiceFactory(
                customer=self.customer, year=2026, month=11
            )
            tasks.set_to_zero_overdue_credits()
            tasks.process_invoice_credits(invoice)
        self.credit.refresh_from_db()
        self.assertEqual(self.credit.value, Decimal("500"))


class MinimalConsumptionTakesGrantedOnlyTest(BaseLotTest):
    """The floor spends granted credit that would go unused; prepayments do
    not expire, so it never takes them. 300 of usage against a floor of 400."""

    def create_credit(self, value="0", **kwargs):
        return factories.CustomerCreditFactory(
            customer=self.customer,
            value=Decimal(value),
            expected_consumption=Decimal("400"),
            apply_as_minimal_consumption=True,
            **kwargs,
        )

    def test_floor_does_not_draw_prepaid_credit(self):
        credit = self.create_credit("30")
        self.top_up(credit, "500")

        self.compensate()

        # Usage takes the 30 granted and 270 paid; the floor finds no
        # granted credit left to take.
        self.assertEqual(
            balances(credit),
            {Kinds.GRANTED: 0, Kinds.PAID: Decimal("230"), Kinds.EARNED: 0},
        )
        self.assert_lots_match_value(credit)

    def test_floor_takes_the_granted_credit_usage_left(self):
        credit = self.create_credit("350")
        self.top_up(credit, "500")

        self.compensate()

        self.assertEqual(
            balances(credit),
            {Kinds.GRANTED: 0, Kinds.PAID: Decimal("500"), Kinds.EARNED: 0},
        )
        self.assert_lots_match_value(credit)

    def test_floor_in_the_end_month_leaves_prepaid_credit(self):
        credit = self.create_credit(
            end_date=datetime.date.today().replace(day=1),
            minimal_consumption_logic=models.CustomerCredit.MinimalConsumptionLogic.LINEAR,
        )
        self.top_up(credit, "500")

        self.compensate()

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("200"))
        self.assertEqual(balances(credit)[Kinds.PAID], Decimal("200"))

    def test_clearing_gives_back_what_the_floor_took(self):
        credit = self.create_credit("350")
        self.top_up(credit, "500")
        self.compensate()

        compensations.MonthlyCompensation(
            self.customer, invoice=self.fixture.invoice
        ).clear_compensations()

        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("350"),
                Kinds.PAID: Decimal("500"),
                Kinds.EARNED: 0,
            },
        )
        self.assert_lots_match_value(credit)


class RepeatedApplyTest(BaseLotTest):
    """Applying a month again, as staff may, first gives back all the last
    apply took, the minimal consumption included."""

    def apply(self, times=1):
        for _ in range(times):
            compensations.MonthlyCompensation(
                self.customer, invoice=self.fixture.invoice
            ).apply_compensations()

    def test_a_month_with_only_the_floor_takes_it_once(self):
        credit = factories.CustomerCreditFactory(
            customer=self.customer,
            value=Decimal("1000"),
            expected_consumption=Decimal("400"),
            apply_as_minimal_consumption=True,
        )
        self.fixture.invoice

        self.apply(times=2)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("600"))
        self.assert_lots_match_value(credit)

    def test_an_allocation_floor_is_taken_once(self):
        self.create_credit("1000")
        project_credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=Decimal("500"),
            expected_consumption=Decimal("400"),
            apply_as_minimal_consumption=True,
        )
        self.fixture.invoice_item

        self.apply(times=3)

        # 300 of usage and the floor's 100.
        project_credit.refresh_from_db()
        self.assertEqual(project_credit.value, Decimal("100"))


class IdleAllocationFloorTest(BaseLotTest):
    """An allocation's own floor takes nothing from the organization credit,
    so a month whose only draw it was still has something to give back.
    The organization holds only paid credit, so its floor takes nothing."""

    def setUp(self):
        super().setUp()
        self.credit = factories.CustomerCreditFactory(
            customer=self.customer,
            value=Decimal("0"),
            expected_consumption=Decimal("300"),
            apply_as_minimal_consumption=True,
        )
        self.top_up(self.credit, "1000")
        self.project_credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=Decimal("500"),
            expected_consumption=Decimal("400"),
            apply_as_minimal_consumption=True,
        )
        # The project ran, but cost nothing, so no compensation is written.
        factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,
            resource=self.fixture.resource,
            unit_price=Decimal("0"),
            quantity=1,
        )

    def apply(self, times=1):
        for _ in range(times):
            compensations.MonthlyCompensation(
                self.customer, invoice=self.fixture.invoice
            ).apply_compensations()

    def test_reapplying_takes_the_allocation_floor_once(self):
        self.apply(times=3)

        self.project_credit.refresh_from_db()
        self.assertEqual(self.project_credit.value, Decimal("100"))

    def test_clearing_gives_back_only_what_was_taken(self):
        # Nothing was drawn from the organization credit, so clearing must not
        # give it the minimal consumption its floor never took.
        self.apply()
        compensations.MonthlyCompensation(
            self.customer, invoice=self.fixture.invoice
        ).clear_compensations()

        self.project_credit.refresh_from_db()
        self.assertEqual(self.project_credit.value, Decimal("500"))
        self.credit.refresh_from_db()
        self.assertEqual(self.credit.value, Decimal("1000"))
        self.assertEqual(balances(self.credit)[Kinds.PAID], Decimal("1000"))
        self.assert_lots_match_value(self.credit)


class UnusedAllocationOnProjectRemovalTest(BaseLotTest):
    """An allocation marked as spent on project removal forfeits only granted
    credit: prepayments and earnings stay with the organization."""

    def setUp(self):
        super().setUp()
        self.credit = self.create_credit("100")
        self.top_up(self.credit, "1000")
        self.adjust_withdrawable(self.credit, "200")
        self.project_credit = factories.ProjectCreditFactory(
            project=self.fixture.project,
            value=Decimal("800"),
            mark_unused_credit_as_spent_on_project_termination=True,
        )

    def test_forfeits_no_more_than_the_granted_credit(self):
        self.fixture.project.delete()

        self.assertEqual(
            balances(self.credit),
            {
                Kinds.GRANTED: 0,
                Kinds.PAID: Decimal("1000"),
                Kinds.EARNED: Decimal("200"),
            },
        )
        self.assert_lots_match_value(self.credit)

    def test_forfeits_the_allocation_when_granted_credit_covers_it(self):
        self.project_credit.value = Decimal("60")
        self.project_credit.save()

        self.fixture.project.delete()

        self.assertEqual(balances(self.credit)[Kinds.GRANTED], Decimal("40"))
        self.assert_lots_match_value(self.credit)


class LinearMinimalConsumptionTest(BaseLotTest):
    def test_only_granted_credit_is_spread(self):
        today = datetime.date.today()
        end_date = (today.replace(day=1) + datetime.timedelta(days=32 * 10)).replace(
            day=1
        )
        credit = factories.CustomerCreditFactory(
            customer=self.customer,
            value=Decimal("1000"),
            end_date=end_date,
            minimal_consumption_logic=models.CustomerCredit.MinimalConsumptionLogic.LINEAR,
        )
        self.top_up(credit, "1000")

        compensations.MonthlyCompensation(
            self.customer, invoice=self.fixture.invoice
        ).update_linear_expected_consumption()

        credit.refresh_from_db()
        self.assertEqual(
            credit.expected_consumption.quantize(Decimal("0.01")),
            (Decimal("1000") * credit.time_left_factor).quantize(Decimal("0.01")),
        )


class ProjectAllocationTest(BaseLotTest):
    def test_the_draw_that_exhausts_the_organization_is_net_of_tax(self):
        invoice = self.fixture.invoice
        invoice.tax_percent = 20
        invoice.save()
        credit = self.create_credit("500")
        factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("500")
        )
        # Spent elsewhere since the allocation was made.
        credit.value = Decimal("100")
        credit.save()
        self.fixture.invoice_item

        compensations.MonthlyCompensation(
            self.customer, invoice=invoice
        ).apply_compensations()

        # As for usage outside an allocation: all 100 left, net of 20% tax.
        compensation = invoice.items.get(credit=credit)
        self.assertEqual(
            compensation.unit_price.quantize(Decimal("0.01")), Decimal("-83.33")
        )
        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)

    def test_allocation_pays_no_more_than_the_organization_has(self):
        credit = self.create_credit("100")
        factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("100")
        )
        other_project = structure_factories.ProjectFactory(customer=self.customer)
        other_resource = marketplace_factories.ResourceFactory(
            project=other_project, offering=self.fixture.resource.offering
        )
        invoice = self.fixture.invoice
        # Drawn cheapest first: 80 with no allocation, then 100 against one.
        factories.InvoiceItemFactory(
            invoice=invoice,
            resource=other_resource,
            project=other_project,
            unit_price=8,
            quantity=10,
        )
        factories.InvoiceItemFactory(
            invoice=invoice,
            resource=self.fixture.resource,
            project=self.fixture.project,
            unit_price=10,
            quantity=10,
        )

        compensations.MonthlyCompensation(
            self.customer, invoice=invoice
        ).apply_compensations()

        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)
        self.assert_lots_match_value(credit)
        compensated = sum(item.price for item in invoice.items.filter(credit=credit))
        self.assertEqual(compensated, Decimal("-100"))


class ReclassifyTest(BaseLotTest):
    def test_staff_moves_granted_credit_to_paid(self):
        credit = self.create_credit("100")
        response = self.reclassify(credit, "60")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        credit.refresh_from_db()
        self.assertEqual(credit.value, Decimal("100"))
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("40"),
                Kinds.PAID: Decimal("60"),
                Kinds.EARNED: 0,
            },
        )
        rows = credit.transactions.filter(
            transaction_type=models.CreditTransaction.Types.RECLASSIFICATION
        )
        self.assertEqual(
            sorted(row.amount for row in rows), [Decimal("-60"), Decimal("60")]
        )
        self.assertTrue(all(row.comment == "Was a prepayment" for row in rows))

    def test_reclassification_is_audited_with_its_comment_and_user(self):
        credit = self.create_credit("100")
        self.reclassify(credit, "60")
        event = Event.objects.filter(
            event_type=EventType.CREDIT_RECLASSIFIED_AS_PAID
        ).get()
        self.assertIn("reclassified as paid: Was a prepayment", event.message)
        self.assertEqual(event.context["user_uuid"], self.staff.uuid.hex)

    def test_cannot_reclassify_more_than_the_granted_balance(self):
        credit = self.create_credit("100")
        self.top_up(credit, "500")
        response = self.reclassify(credit, "101")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_owner_cannot_reclassify(self):
        credit = self.create_credit("100")
        response = self.reclassify(credit, "10", user=self.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_comment_is_required(self):
        credit = self.create_credit("100")
        response = self.reclassify(credit, "10", comment="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class LotsMatchValueTest(BaseLotTest):
    def test_value_is_the_sum_of_lots_through_a_month(self):
        credit = self.create_credit("100")
        self.top_up(credit, "400")
        self.adjust_withdrawable(credit, "50")
        self.assert_lots_match_value(credit)

        self.compensate()
        self.assert_lots_match_value(credit)

        credit.expected_consumption = Decimal("350")
        credit.save()
        self.compensate()
        self.assert_lots_match_value(credit)

        self.adjust_withdrawable(credit, "-20", comment="payout")
        self.assert_lots_match_value(credit)


class MigrationTest(BaseLotTest):
    def test_existing_balance_splits_into_earned_and_granted(self):
        credit = self.create_credit("100")
        self.adjust_withdrawable(credit, "30")
        credit.refresh_from_db()
        withdrawable_before = credit.withdrawable_balance
        models.CreditLot.objects.filter(credit=credit).delete()

        migration = importlib.import_module(
            "waldur_mastermind.invoices.migrations.0037_credit_lots"
        )
        migration.create_lots(apps, None)

        credit.refresh_from_db()
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("100"),
                Kinds.PAID: 0,
                Kinds.EARNED: Decimal("30"),
            },
        )
        self.assertEqual(credit.withdrawable_balance, withdrawable_before)


class ResplitTest(BaseLotTest):
    """Rebuilding lots after an import wrote the value past the ledger handler."""

    def setUp(self):
        super().setUp()
        self.credit = self.create_credit("100")
        self.top_up(self.credit, "50")
        self.adjust_withdrawable(self.credit, "30")

    def test_lots_are_rebuilt_from_the_ledger(self):
        models.CreditLot.objects.filter(credit=self.credit).delete()
        self.credit.refresh_from_db()
        lots.resplit(self.credit)
        self.assertEqual(
            balances(self.credit),
            {
                Kinds.GRANTED: Decimal("100"),
                Kinds.PAID: Decimal("50"),
                Kinds.EARNED: Decimal("30"),
            },
        )

    def test_spent_earnings_stay_spent(self):
        # Earnings spent on usage, then a grant: a rebuild must
        # not make the grant withdrawable.
        self.credit.delete()
        credit = self.create_credit()
        self.adjust_withdrawable(credit, "100")
        self.compensate()
        credit.refresh_from_db()
        credit.value += 100
        credit.save()
        lots.resplit(credit)
        self.assertEqual(credit.withdrawable_balance, Decimal("0"))
        self.assertEqual(credit.granted_balance, Decimal("100"))

    def test_reclassification_survives_a_rebuild(self):
        self.credit.delete()
        credit = self.create_credit("100")
        self.reclassify(credit, "60")
        lots.resplit(credit)
        self.assertEqual(
            balances(credit),
            {
                Kinds.GRANTED: Decimal("40"),
                Kinds.PAID: Decimal("60"),
                Kinds.EARNED: 0,
            },
        )

    def test_a_value_drawn_down_past_the_handler_takes_granted_first(self):
        models.CustomerCredit.objects.filter(pk=self.credit.pk).update(
            value=Decimal("60")
        )
        self.credit.refresh_from_db()
        lots.resplit(self.credit)
        self.assertEqual(
            balances(self.credit),
            {
                Kinds.GRANTED: 0,
                Kinds.PAID: Decimal("30"),
                Kinds.EARNED: Decimal("30"),
            },
        )


class PaidCreditAffiliateMixin:
    """The referred customer's invoice is 300; the affiliate earns 10%."""

    def setUp(self):
        self.fixture = fixtures.AffiliateFixture()
        self.fixture.affiliate_link
        self.customer = self.fixture.customer
        self.staff = structure_factories.UserFactory(is_staff=True)

    def credit(self, granted="0", paid="0"):
        credit = factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal(granted)
        )
        if Decimal(paid):
            self.client.force_authenticate(self.staff)
            response = self.client.post(
                factories.CustomerCreditFactory.get_url(credit, action="top_up"),
                {"amount": paid, "comment": "Bank transfer"},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return credit

    def finalize(self, apply_times=1):
        self.fixture.invoice_item
        for _ in range(apply_times):
            tasks.process_invoice_credits(self.fixture.invoice)
        self.fixture.invoice.set_created()

    def fee(self):
        accrual = models.AffiliateFeeAccrual.objects.filter(
            invoice=self.fixture.invoice
        ).first()
        return accrual.amount if accrual else Decimal("0")


@override_config(AFFILIATES_ENABLED=True)
class AffiliateFeeOnPaidCreditTest(PaidCreditAffiliateMixin, test.APITestCase):
    def test_no_credit(self):
        self.finalize()
        self.assertEqual(self.fee(), Decimal("30"))

    def test_invoice_fully_paid_from_paid_credit(self):
        self.credit(paid="500")
        self.finalize()
        self.assertEqual(self.fixture.invoice.price, Decimal("0"))
        self.assertEqual(self.fee(), Decimal("30"))

    def test_invoice_partly_paid_from_paid_credit(self):
        self.credit(paid="100")
        self.finalize()
        self.assertEqual(self.fixture.invoice.price, Decimal("200"))
        self.assertEqual(self.fee(), Decimal("30"))

    def test_granted_credit_does_not_count(self):
        self.credit(granted="100")
        self.finalize()
        self.assertEqual(self.fee(), Decimal("20"))

    def test_fully_granted_invoice_earns_nothing(self):
        self.credit(granted="500")
        self.finalize()
        self.assertEqual(self.fee(), Decimal("0"))

    def test_mixed_credit_counts_only_the_paid_part(self):
        # 50 granted is drawn first, then 100 paid: price 150, base 250.
        self.credit(granted="50", paid="100")
        self.finalize()
        self.assertEqual(self.fee(), Decimal("25"))

    def test_reapplied_compensations_count_once(self):
        self.credit(paid="100")
        self.finalize(apply_times=3)
        self.assertEqual(self.fee(), Decimal("30"))

    def test_tax_comes_off_the_last_draw_only(self):
        # Items of 100 and 300 with 25% VAT, credit of 100 granted and 20
        # paid: the 100 item takes the granted 100 exactly; the 300 item
        # takes the paid 20, which compensates 20 / 1.25 = 16 net.
        self.fixture.invoice.tax_percent = Decimal("25")
        self.fixture.invoice.save(update_fields=["tax_percent"])
        factories.InvoiceItemFactory(
            invoice=self.fixture.invoice,
            project=self.fixture.project,
            resource=self.fixture.resource,
            unit_price=Decimal("100"),
            quantity=1,
        )
        self.credit(granted="100", paid="20")
        self.finalize()
        self.assertEqual(self.fixture.invoice.price, Decimal("284"))
        self.assertEqual(self.fee(), Decimal("30"))

    def test_last_partial_draw_is_counted_net_of_tax(self):
        # With 20% VAT an exhausted credit compensates 120 / 1.2 = 100 net.
        self.fixture.invoice.tax_percent = Decimal("20")
        self.fixture.invoice.save(update_fields=["tax_percent"])
        self.credit(paid="120")
        self.finalize()
        self.assertEqual(self.fixture.invoice.price, Decimal("200"))
        self.assertEqual(self.fee(), Decimal("30"))


@ddt
@override_config(AFFILIATES_ENABLED=True)
class UpcomingFeeMatchesAccrualTest(PaidCreditAffiliateMixin, test.APITestCase):
    """The fee projected on the open invoice is the fee accrued at its close:
    the projection counts what paid credit will cover as the accrual does."""

    def projected(self):
        self.fixture.invoice_item
        return self.fixture.affiliate_link.calculate_fee(
            compensations.projected_fee_base(self.fixture.invoice)
        )

    def apply_mid_month(self):
        self.fixture.invoice_item
        tasks.process_invoice_credits(self.fixture.invoice)

    @data(
        # (granted, paid, tax percent, extra 100 item, applied mid-month, fee)
        ("0", "0", "0", False, False, "30"),
        ("0", "500", "0", False, False, "30"),
        ("0", "100", "0", False, False, "30"),
        ("100", "0", "0", False, False, "20"),
        ("500", "0", "0", False, False, "0"),
        ("50", "100", "0", False, False, "25"),
        ("50", "100", "0", False, True, "25"),
        ("0", "120", "20", False, False, "30"),
        ("100", "20", "25", True, False, "30"),
    )
    @unpack
    def test_projection_equals_accrual(
        self, granted, paid, tax, extra_item, mid_month, fee
    ):
        if Decimal(tax):
            self.fixture.invoice.tax_percent = Decimal(tax)
            self.fixture.invoice.save(update_fields=["tax_percent"])
        if extra_item:
            factories.InvoiceItemFactory(
                invoice=self.fixture.invoice,
                project=self.fixture.project,
                resource=self.fixture.resource,
                unit_price=Decimal("100"),
                quantity=1,
            )
        self.credit(granted=granted, paid=paid)
        if mid_month:
            self.apply_mid_month()

        projected = self.projected()
        self.finalize()

        self.assertEqual(projected, Decimal(fee))
        self.assertEqual(self.fee(), projected)


@freeze_time("2026-10-15")
@override_config(AFFILIATES_ENABLED=True)
class AffiliateFeeOnEarlierInvoicePaymentsTest(test.APITestCase):
    """September closed with 1000 that no credit covered, and its fee was
    accrued then. October costs 200; at month end the project credit pays
    October and then what it has left of September, onto October's invoice."""

    def setUp(self):
        self.fixture = fixtures.AffiliateFixture()
        self.fixture.affiliate_link
        self.customer = self.fixture.customer
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.september = self.invoice(9, models.Invoice.States.CREATED)
        self.item(self.september, "1000")
        self.october = self.invoice(10, models.Invoice.States.PENDING)
        self.item(self.october, "200")

    def invoice(self, month, state):
        return factories.InvoiceFactory(
            customer=self.customer, year=2026, month=month, state=state
        )

    def item(self, invoice, price):
        factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            resource=self.fixture.resource,
            unit_price=Decimal(price),
            quantity=1,
        )

    def credit(self, granted="0", paid="0"):
        credit = factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal(granted)
        )
        if Decimal(paid):
            self.client.force_authenticate(self.staff)
            response = self.client.post(
                factories.CustomerCreditFactory.get_url(credit, action="top_up"),
                {"amount": paid, "comment": "Bank transfer"},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        factories.ProjectCreditFactory(
            project=self.fixture.project, value=1000, pay_earlier_invoices=True
        )
        return credit

    def finalize(self):
        tasks.process_invoice_credits(self.october)
        self.october.set_created()
        self.october.refresh_from_db()
        accrual = models.AffiliateFeeAccrual.objects.filter(
            invoice=self.october
        ).first()
        return accrual.amount if accrual else Decimal("0")

    def test_paid_payment_of_september_does_not_lower_octobers_fee(self):
        self.credit(paid="1000")
        fee = self.finalize()
        # 200 for October, then 800 of September onto October's invoice.
        self.assertEqual(self.october.price, Decimal("-800"))
        self.assertEqual(fee, Decimal("20"))

    def test_granted_payment_of_september_takes_its_fee_back(self):
        self.credit(granted="1000")
        self.assertEqual(self.finalize(), Decimal("0"))

    def test_only_the_paid_share_of_each_draw_counts(self):
        # October's 200 is 100 granted and 100 paid; September's 800 is
        # paid: the base is -800 + 100 + 800.
        credit = self.credit(granted="100", paid="1000")
        self.assertEqual(self.finalize(), Decimal("10"))
        self.assertEqual(
            balances(credit),
            {Kinds.GRANTED: 0, Kinds.PAID: Decimal("100"), Kinds.EARNED: 0},
        )
