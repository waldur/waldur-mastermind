"""Staff withdrawable-balance adjustments and the credit-transaction trace."""

import datetime
from decimal import Decimal

from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.tests import factories, fixtures


class WithdrawableAdjustmentTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.credit = factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("100")
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.url = factories.CustomerCreditFactory.get_url(
            self.credit, action="adjust_withdrawable"
        )

    def _adjust(self, user, amount, comment="manual grant"):
        self.client.force_authenticate(user)
        return self.client.post(self.url, {"amount": amount, "comment": comment})

    def test_staff_adjustment_changes_value_and_withdrawable(self):
        response = self._adjust(self.staff, "40")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.credit.refresh_from_db()
        self.assertEqual(self.credit.value, Decimal("140"))
        self.assertEqual(self.credit.withdrawable_balance, Decimal("40"))
        tx = models.CreditTransaction.objects.get(
            credit=self.credit,
            transaction_type=models.CreditTransaction.Types.WITHDRAWABLE_ADJUSTMENT,
        )
        self.assertEqual(tx.amount, Decimal("40"))
        self.assertEqual(tx.comment, "manual grant")

    def test_negative_adjustment(self):
        self._adjust(self.staff, "40")
        response = self._adjust(self.staff, "-10", comment="clawback")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.credit.refresh_from_db()
        self.assertEqual(self.credit.value, Decimal("130"))
        self.assertEqual(self.credit.withdrawable_balance, Decimal("30"))

    def test_negative_adjustment_cannot_exceed_withdrawable_balance(self):
        # Grant 40 of withdrawable credit; the withdrawable balance is now 40
        # while the total value is 140 (100 of it staff-granted, non-withdrawable).
        self._adjust(self.staff, "40")
        # A reduction larger than the withdrawable balance is rejected so it
        # cannot draw down the staff-granted portion.
        response = self._adjust(self.staff, "-60", comment="over-payout")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.credit.refresh_from_db()
        self.assertEqual(self.credit.value, Decimal("140"))
        self.assertEqual(self.credit.withdrawable_balance, Decimal("40"))

    def test_owner_cannot_adjust(self):
        self.assertEqual(
            self._adjust(self.owner, "40").status_code, status.HTTP_403_FORBIDDEN
        )

    def test_zero_amount_rejected(self):
        self.assertEqual(
            self._adjust(self.staff, "0").status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_comment_required(self):
        self.assertEqual(
            self._adjust(self.staff, "40", comment="").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_cannot_make_value_negative(self):
        self.assertEqual(
            self._adjust(self.staff, "-500").status_code,
            status.HTTP_400_BAD_REQUEST,
        )


class CreditTransactionTraceTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.credit = factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("0")
        )
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.other = structure_factories.UserFactory()
        self.list_url = reverse("credit-transaction-list")
        # Seed one transaction via a staff withdrawable adjustment.
        self.client.force_authenticate(self.staff)
        self.client.post(
            factories.CustomerCreditFactory.get_url(
                self.credit, action="adjust_withdrawable"
            ),
            {"amount": "25", "comment": "seed"},
        )

    def test_staff_sees_transactions(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.list_url, {"credit_uuid": self.credit.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["comment"], "seed")
        self.assertEqual(str(response.data[0]["amount"]), "25.00000")

    def test_owner_sees_own_transactions(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            self.list_url, {"customer_uuid": self.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 1)

    def test_unrelated_user_sees_nothing(self):
        self.client.force_authenticate(self.other)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class ProjectCreditTransactionTraceTest(test.APITestCase):
    """A project allocation's drawdown is half the ledger, and the half the
    project dashboard is about. It has to be readable by the people the
    dashboard is for, and carry enough on the row to group and attribute it."""

    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.fixture.customer_credit.value = Decimal("100")
        self.fixture.customer_credit.save(update_fields=["value"])
        self.project_credit = factories.ProjectCreditFactory(
            project=self.project, value=Decimal("40")
        )
        self.period = datetime.date(2024, 3, 1)
        self.row = models.CreditTransaction.objects.create(
            project_credit=self.project_credit,
            project_uuid=self.project.uuid.hex,
            project_name=self.project.name,
            amount=Decimal("-10"),
            transaction_type=models.CreditTransaction.Types.COMPENSATION,
            billing_period=self.period,
        )
        self.list_url = reverse("credit-transaction-list")

    def get(self, user, **query):
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def uuids(self, rows):
        return {row["uuid"] for row in rows}

    def test_owner_sees_project_rows(self):
        self.assertIn(self.row.uuid.hex, self.uuids(self.get(self.fixture.owner)))

    def test_project_role_sees_its_own_project_rows(self):
        # Mirrors ProjectCredit itself, which project roles can already read so
        # the project dashboard has something to render.
        self.assertIn(self.row.uuid.hex, self.uuids(self.get(self.fixture.manager)))

    def test_unrelated_user_sees_nothing(self):
        self.assertEqual(self.get(structure_factories.UserFactory()), [])

    def test_row_carries_its_project_and_month(self):
        (row,) = [
            row
            for row in self.get(self.fixture.owner)
            if row["uuid"] == self.row.uuid.hex
        ]
        self.assertEqual(row["project_uuid"], self.project.uuid.hex)
        self.assertEqual(row["project_name"], self.project.name)
        self.assertEqual(row["billing_period"], self.period)
        # The organization is still named on a project row: a client scoping the
        # ledger to one organization has nothing else to filter on.
        self.assertEqual(row["customer_name"], self.customer.name)

    def test_rows_can_be_narrowed_to_a_project_and_a_month(self):
        other = factories.ProjectCreditFactory(
            project=structure_factories.ProjectFactory(customer=self.customer)
        )
        other_row = models.CreditTransaction.objects.create(
            project_credit=other,
            project_uuid=other.project.uuid.hex,
            project_name=other.project.name,
            amount=Decimal("-5"),
            transaction_type=models.CreditTransaction.Types.MINIMAL_DRAW,
            billing_period=self.period,
        )

        by_project = self.uuids(
            self.get(self.fixture.owner, project_uuid=self.project.uuid.hex)
        )
        self.assertIn(self.row.uuid.hex, by_project)
        self.assertNotIn(other_row.uuid.hex, by_project)

        # The allocations were granted outside any billing month, so only the
        # drawdown rows answer to a month.
        by_month = self.uuids(self.get(self.fixture.owner, billing_period="2024-03-01"))
        self.assertEqual(by_month, {self.row.uuid.hex, other_row.uuid.hex})
        self.assertEqual(self.get(self.fixture.owner, billing_period="2024-04-01"), [])

    def test_a_row_outliving_its_allocation_still_names_its_project(self):
        self.project_credit.delete()

        (row,) = [
            row
            for row in self.get(self.fixture.staff)
            if row["uuid"] == self.row.uuid.hex
        ]
        self.assertEqual(row["project_uuid"], self.project.uuid.hex)
        self.assertIsNone(row["customer_uuid"])
