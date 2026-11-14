import decimal
from unittest import mock

from django.test import TestCase
from freezegun import freeze_time

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_openportal import utils


@freeze_time("2025-10-15")
class SetProjectCreditsWritesOnlyOnChangeTest(TestCase):
    """
    A second run with nothing changed must not rewrite the credit: each write
    logs a credit change, and the nightly reconciliation rewrote every project
    its invoices had drawn on.
    """

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        invoice_models.CustomerCredit.objects.create(
            customer=self.project.customer, value=decimal.Decimal(100000)
        )
        # Credit already drawn down by an invoice, as compensations record it.
        invoice = invoice_factories.InvoiceFactory(
            customer=self.project.customer, year=2025, month=9
        )
        invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.project,
            unit_price=decimal.Decimal("-300"),
            quantity=1,
        )

    def test_an_unchanged_credit_is_not_rewritten(self):
        utils.set_project_credits(self.project, decimal.Decimal(1000))
        with mock.patch.object(invoice_models.ProjectCredit, "save") as save:
            utils.set_project_credits(self.project, decimal.Decimal(1000))

        save.assert_not_called()

    def test_a_changed_allocation_is_still_written(self):
        utils.set_project_credits(self.project, decimal.Decimal(1000))
        utils.set_project_credits(self.project, decimal.Decimal(1500))

        credit = invoice_models.ProjectCredit.objects.get(project=self.project)
        self.assertEqual(credit.value, decimal.Decimal(1500))

    def test_the_stored_value_has_the_fields_precision(self):
        utils.set_project_credits(self.project, decimal.Decimal("1000.123456789"))

        credit = invoice_models.ProjectCredit.objects.get(project=self.project)
        self.assertEqual(credit.value, decimal.Decimal("1000.12346"))
        self.assertEqual(credit.value.as_tuple().exponent, -5)

    def test_a_raise_tops_up_the_organization_by_the_real_increase(self):
        # The project holds 700 and the organization exactly that, so a raise
        # to 1500 needs 800 more. Measured against the stored value plus the
        # 300 already drawn down, the top-up came out 300 short and the save
        # failed validation, retry included.
        invoice_models.CustomerCredit.objects.filter(
            customer=self.project.customer
        ).update(value=decimal.Decimal(700))
        invoice_models.ProjectCredit.objects.create(
            project=self.project, value=decimal.Decimal(700)
        )

        utils.set_project_credits(self.project, decimal.Decimal(1500))

        credit = invoice_models.ProjectCredit.objects.get(project=self.project)
        self.assertEqual(credit.value, decimal.Decimal(1500))
