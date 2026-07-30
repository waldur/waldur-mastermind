from decimal import Decimal

from django.db.models import Sum
from rest_framework import test

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import fixtures as invoice_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.demo_presets.credit_history import (
    generate_credit_history,
)


class CreditHistoryGenerationTest(test.APITestCase):
    """The generated demo data must obey the invariants the real compensation
    flow guarantees. Hand-authored credit fixtures have violated these before,
    which made demo data look healthier than production.
    """

    def setUp(self):
        self.fixture = invoice_fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        self.customer = self.fixture.customer
        # Only resources in the OK state are billable, matching the preset
        # loader's own billing step.
        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save(update_fields=["state"])
        generate_credit_history(months=3)

    def _compensations(self):
        return invoice_models.InvoiceItem.objects.filter(
            credit=self.customer_credit, unit_price__lt=0
        )

    def test_history_is_generated(self):
        self.assertTrue(
            self._compensations().exists(),
            "no compensation was produced for a credited customer",
        )

    def test_compensation_never_exceeds_the_cost_it_offsets(self):
        for invoice in invoice_models.Invoice.objects.filter(customer=self.customer):
            incurred = invoice.items.filter(unit_price__gt=0).aggregate(
                total=Sum("unit_price")
            )["total"] or Decimal("0")
            compensated = -(
                invoice.items.filter(unit_price__lt=0).aggregate(
                    total=Sum("unit_price")
                )["total"]
                or Decimal("0")
            )
            self.assertLessEqual(
                compensated,
                incurred,
                f"{invoice.year}-{invoice.month}: compensation exceeds incurred cost, "
                "which the real flow cannot produce",
            )

    def test_compensations_are_attributed_to_their_project(self):
        for item in self._compensations():
            self.assertTrue(
                item.project_uuid,
                "compensation is missing project_uuid and would be invisible "
                "to the project-scoped costs endpoint",
            )

    def test_generation_is_idempotent(self):
        before = self._compensations().count()
        generate_credit_history(months=3)
        self.assertEqual(
            before,
            self._compensations().count(),
            "re-running duplicated invoice items",
        )

    def test_no_credit_means_no_history(self):
        invoice_models.InvoiceItem.objects.all().delete()
        invoice_models.Invoice.objects.all().delete()
        invoice_models.ProjectCredit.objects.all().delete()
        invoice_models.CustomerCredit.objects.all().delete()
        self.assertEqual(generate_credit_history(months=3), 0)
