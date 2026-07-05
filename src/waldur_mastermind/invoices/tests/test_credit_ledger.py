"""Staff withdrawable-balance adjustments and the credit-transaction trace."""

from decimal import Decimal

from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.tests import factories


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
