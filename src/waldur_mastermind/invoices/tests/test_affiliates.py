import decimal

from constance.test import override_config
from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.invoices.handlers import process_affiliate_fees
from waldur_mastermind.invoices.tests import factories, fixtures


@override_config(AFFILIATES_ENABLED=True)
class BaseAffiliateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.AffiliateFixture()
        self.link = self.fixture.affiliate_link

    def finalize_invoice(self):
        # Materialize the invoice item before finalization so the invoice
        # has a non-zero price, then run the real PENDING -> CREATED
        # transition which emits the invoice_created signal.
        self.fixture.invoice_item
        self.fixture.invoice.set_created()
        return self.fixture.invoice

    @property
    def affiliate_credit(self):
        return models.CustomerCredit.objects.filter(
            customer=self.fixture.affiliate_customer
        ).first()


@ddt
class AffiliateRetrieveTest(BaseAffiliateTest):
    def get_link(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.get(factories.CustomerAffiliateFactory.get_url(self.link))

    @data("staff", "global_support", "affiliate_owner")
    def test_user_with_access_can_retrieve_link(self, user):
        response = self.get_link(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "manager", "admin", "user")
    def test_other_users_cannot_retrieve_link(self, user):
        # Not even the referred customer's owner: the link is visible to
        # staff and the affiliate organization only.
        response = self.get_link(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class AffiliateCreateTest(BaseAffiliateTest):
    def create_link(self, user, **kwargs):
        payload = {
            "customer": structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
            "affiliate": structure_factories.CustomerFactory.get_url(
                structure_factories.CustomerFactory()
            ),
            "fee_percent": 10,
        }
        payload.update(kwargs)
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(
            factories.CustomerAffiliateFactory.get_list_url(), payload
        )

    def test_staff_can_create_link(self):
        response = self.create_link("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("affiliate_owner", "owner", "user")
    def test_non_staff_cannot_create_link(self, user):
        response = self.create_link(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_self_affiliation_is_rejected(self):
        response = self.create_link(
            "staff",
            affiliate=structure_factories.CustomerFactory.get_url(
                self.fixture.customer
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_percent_fee_above_100_is_rejected(self):
        response = self.create_link("staff", fee_percent=101)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_braces_in_referred_customer_name_do_not_break_audit_log(self):
        # The audit handler must not feed user data into the event template's
        # .format() call: a name with braces would otherwise raise.
        referred = structure_factories.CustomerFactory(name="Acme {EU} GmbH")
        response = self.create_link(
            "staff",
            customer=structure_factories.CustomerFactory.get_url(referred),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class AffiliateUpdateTest(BaseAffiliateTest):
    def update_link(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(
            factories.CustomerAffiliateFactory.get_url(self.link),
            {"fee_percent": 50},
        )

    def test_staff_can_update_terms(self):
        response = self.update_link("staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.link.refresh_from_db()
        self.assertEqual(self.link.fee_percent, 50)

    def test_affiliate_owner_cannot_update_own_terms(self):
        response = self.update_link("affiliate_owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.link.refresh_from_db()
        self.assertEqual(self.link.fee_percent, 10)


class AffiliateFeeAccrualTest(BaseAffiliateTest):
    def test_percent_fee_is_accrued_on_invoice_finalization(self):
        invoice = self.finalize_invoice()
        # invoice price: 10 x 30 = 300, fee: 10% = 30
        credit = self.affiliate_credit
        self.assertIsNotNone(credit)
        self.assertEqual(credit.value, decimal.Decimal("30"))

        accrual = models.AffiliateFeeAccrual.objects.get(
            affiliate_link=self.link, invoice=invoice
        )
        self.assertEqual(accrual.amount, decimal.Decimal("30"))

    def test_accrual_is_idempotent(self):
        invoice = self.finalize_invoice()
        process_affiliate_fees(sender=models.Invoice, invoice=invoice)
        process_affiliate_fees(sender=models.Invoice, invoice=invoice)

        self.assertEqual(
            models.AffiliateFeeAccrual.objects.filter(affiliate_link=self.link).count(),
            1,
        )
        self.assertEqual(self.affiliate_credit.value, decimal.Decimal("30"))

    def test_updated_percent_changes_fee(self):
        self.link.fee_percent = decimal.Decimal("25")
        self.link.save()
        self.finalize_invoice()
        # 25% of 300 = 75
        self.assertEqual(self.affiliate_credit.value, decimal.Decimal("75"))

    def test_inactive_link_does_not_accrue(self):
        self.link.is_active = False
        self.link.save()
        self.finalize_invoice()
        self.assertIsNone(self.affiliate_credit)

    def test_zero_invoice_does_not_accrue(self):
        self.fixture.invoice.set_created()
        self.assertIsNone(self.affiliate_credit)

    def test_existing_affiliate_credit_is_incremented(self):
        factories.CustomerCreditFactory(
            customer=self.fixture.affiliate_customer, value=decimal.Decimal("100")
        )
        self.finalize_invoice()
        self.assertEqual(self.affiliate_credit.value, decimal.Decimal("130"))


class CreditLedgerTest(BaseAffiliateTest):
    def test_staff_grant_is_recorded(self):
        credit = factories.CustomerCreditFactory(
            customer=self.fixture.affiliate_customer, value=decimal.Decimal("100")
        )
        transaction = credit.transactions.get()
        self.assertEqual(
            transaction.transaction_type,
            models.CreditTransaction.Types.STAFF_GRANT,
        )
        self.assertEqual(transaction.amount, decimal.Decimal("100"))

        credit.value = decimal.Decimal("80")
        credit.save(update_fields=["value"])
        self.assertEqual(credit.transactions.count(), 2)
        self.assertEqual(
            credit.transactions.order_by("created").last().amount,
            decimal.Decimal("-20"),
        )

    def test_affiliate_fee_is_recorded_and_withdrawable(self):
        self.finalize_invoice()
        credit = self.affiliate_credit
        transaction = credit.transactions.get(
            transaction_type=models.CreditTransaction.Types.AFFILIATE_FEE
        )
        self.assertEqual(transaction.amount, decimal.Decimal("30"))
        self.assertEqual(credit.withdrawable_balance, decimal.Decimal("30"))

    def test_staff_grant_is_not_withdrawable(self):
        credit = factories.CustomerCreditFactory(
            customer=self.fixture.affiliate_customer, value=decimal.Decimal("1000")
        )
        self.assertEqual(credit.withdrawable_balance, decimal.Decimal("0"))

        self.finalize_invoice()
        credit.refresh_from_db()
        # Only the earned part of the mixed balance is withdrawable.
        self.assertEqual(credit.value, decimal.Decimal("1030"))
        self.assertEqual(credit.withdrawable_balance, decimal.Decimal("30"))

    def test_expiry_is_recorded_and_wipes_withdrawable_balance(self):
        with freeze_time("2024-03-15"):
            self.finalize_invoice()
            credit = self.affiliate_credit
            credit.end_date = credit.created.date().replace(day=1)
            credit.save(update_fields=["end_date"])

            tasks.set_to_zero_overdue_credits(
                effective_date=credit.created.date().replace(day=15)
            )

        credit.refresh_from_db()
        self.assertEqual(credit.value, 0)
        self.assertEqual(credit.withdrawable_balance, decimal.Decimal("0"))
        expiry = credit.transactions.get(
            transaction_type=models.CreditTransaction.Types.EXPIRY
        )
        self.assertEqual(expiry.amount, decimal.Decimal("-30"))


class AffiliatePrivacyTest(BaseAffiliateTest):
    def test_affiliate_owner_sees_earnings_but_not_invoice(self):
        invoice = self.finalize_invoice()
        self.client.force_authenticate(self.fixture.affiliate_owner)

        response = self.client.get(
            factories.CustomerAffiliateFactory.get_url(self.link, action="accruals")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        accrual = response.data[0]
        self.assertEqual(decimal.Decimal(accrual["amount"]), decimal.Decimal("30"))
        self.assertEqual(accrual["invoice_year"], int(invoice.year))
        self.assertEqual(accrual["invoice_month"], int(invoice.month))
        # The accrual payload must not link or embed the invoice.
        self.assertNotIn("invoice", accrual)
        self.assertNotIn("url", accrual)

        # The referred customer's invoice itself stays out of reach.
        response = self.client.get(factories.InvoiceFactory.get_url(invoice))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_earnings_endpoint(self):
        self.finalize_invoice()
        self.client.force_authenticate(self.fixture.affiliate_owner)
        response = self.client.get(
            factories.CustomerAffiliateFactory.get_url(self.link, action="earnings")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            decimal.Decimal(response.data["total_earned"]), decimal.Decimal("30")
        )
        self.assertEqual(
            decimal.Decimal(response.data["withdrawable_balance"]),
            decimal.Decimal("30"),
        )
        self.assertEqual(len(response.data["per_month"]), 1)

    def test_referred_customer_owner_does_not_see_affiliate_links(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.CustomerAffiliateFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class AffiliateFeatureToggleTest(BaseAffiliateTest):
    """The affiliate program is opt-in via the AFFILIATES_ENABLED Constance
    setting: when disabled (the default), the API responds 404 and no fees
    are accrued."""

    @override_config(AFFILIATES_ENABLED=False)
    def test_api_is_hidden_when_feature_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(factories.CustomerAffiliateFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.get(
            factories.CustomerAffiliateFactory.get_url(self.link)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.post(
            factories.CustomerAffiliateFactory.get_list_url(),
            {
                "customer": structure_factories.CustomerFactory.get_url(
                    self.fixture.customer
                ),
                "affiliate": structure_factories.CustomerFactory.get_url(
                    structure_factories.CustomerFactory()
                ),
                "fee_percent": 10,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_config(AFFILIATES_ENABLED=False)
    def test_no_fee_is_accrued_when_feature_is_disabled(self):
        invoice = self.finalize_invoice()
        self.assertEqual(invoice.state, models.Invoice.States.CREATED)
        self.assertIsNone(self.affiliate_credit)
        self.assertFalse(models.AffiliateFeeAccrual.objects.exists())

    def test_accrual_resumes_when_feature_is_enabled_again(self):
        with override_config(AFFILIATES_ENABLED=False):
            invoice = self.finalize_invoice()
            self.assertFalse(models.AffiliateFeeAccrual.objects.exists())

        # Re-enabling and re-processing the already-finalized invoice
        # accrues the fee exactly once.
        process_affiliate_fees(sender=models.Invoice, invoice=invoice)
        self.assertEqual(self.affiliate_credit.value, decimal.Decimal("30"))


@override_config(AFFILIATES_ENABLED=True)
class CustomerHasAffiliateLinksFlagTest(BaseAffiliateTest):
    """The customer serializer exposes ``has_affiliate_links`` so the UI can
    hide the affiliate earnings view for organizations that earn nothing."""

    def _get_flag(self, customer):
        self.client.force_authenticate(self.fixture.staff)
        url = structure_factories.CustomerFactory.get_url(customer)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["has_affiliate_links"]

    def test_true_for_the_affiliate_organization(self):
        self.assertTrue(self._get_flag(self.fixture.affiliate_customer))

    def test_false_for_a_referred_only_organization(self):
        # The referred customer is on the link but is not the affiliate.
        self.assertFalse(self._get_flag(self.fixture.customer))

    def test_false_for_an_unrelated_organization(self):
        self.assertFalse(self._get_flag(structure_factories.CustomerFactory()))

    @override_config(AFFILIATES_ENABLED=False)
    def test_false_when_feature_is_disabled(self):
        self.assertFalse(self._get_flag(self.fixture.affiliate_customer))
