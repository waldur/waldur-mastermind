"""What the open month will draw from a project credit.

The project dashboard paces the running month against the credit's expected
consumption. It had no credit-scoped figure to pace with, so it used the whole
project invoice — every line, whether or not the credit covers the offering it
came from. On a deployment where compute is credit-funded and storage is not,
that reported a draw several times the real one, and the pace verdict with it.

`creditable_cost_this_month` answers the question the dashboard was asking:
of the cost booked so far, how much is this credit eligible to be drawn
against. It applies the same eligibility rule as the compensation run, which
is why both now read it from one place.
"""

from decimal import Decimal

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@freeze_time("2024-02-05")
class CreditableCostThisMonthTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        self.invoice = factories.InvoiceFactory(
            customer=self.fixture.customer, year=2024, month=2
        )
        self.covered_offering = marketplace_factories.OfferingFactory()
        self.customer_credit.offerings.add(self.covered_offering)

    def add_item(self, offering, price, project=None, **kwargs):
        resource = marketplace_factories.ResourceFactory(
            offering=offering, project=project or self.fixture.project
        )
        return factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=resource,
            project=project or self.fixture.project,
            unit_price=Decimal(price),
            quantity=1,
            **kwargs,
        )

    def cost(self):
        return self.project_credit.creditable_cost_this_month

    def test_cost_on_a_covered_offering_counts(self):
        self.add_item(self.covered_offering, 100)
        self.assertEqual(self.cost(), Decimal(100))

    def test_cost_on_an_offering_the_credit_does_not_cover_is_excluded(self):
        # The reported defect: storage billed outside the credit's offerings
        # was counted as credit draw, inflating the month several times over.
        self.add_item(self.covered_offering, 100)
        self.add_item(marketplace_factories.OfferingFactory(), 900)
        self.assertEqual(self.cost(), Decimal(100))

    def test_an_empty_offering_list_covers_everything(self):
        # Empty means unrestricted, not "nothing" — the same reading the
        # compensation run takes. Getting this backwards would report zero
        # draw for every credit that names no offering.
        self.customer_credit.offerings.clear()
        self.add_item(self.covered_offering, 100)
        self.add_item(marketplace_factories.OfferingFactory(), 900)
        self.assertEqual(self.cost(), Decimal(1000))

    def test_an_item_without_a_resource_is_excluded(self):
        # Nothing attributes it to an offering, so no credit covers it.
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.fixture.project,
            unit_price=Decimal(900),
            quantity=1,
        )
        self.add_item(self.covered_offering, 100)
        self.assertEqual(self.cost(), Decimal(100))

    def test_another_projects_cost_is_excluded(self):
        other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        self.add_item(self.covered_offering, 900, project=other_project)
        self.add_item(self.covered_offering, 100)
        self.assertEqual(self.cost(), Decimal(100))

    def test_a_volume_discount_reduces_the_item_it_is_paired_with(self):
        item = self.add_item(self.covered_offering, 100)
        self.add_item(
            self.covered_offering,
            -60,
            details={"is_discount": True, "discount_of_item": item.uuid.hex},
        )
        self.assertEqual(self.cost(), Decimal(40))

    def test_a_discount_never_drives_the_cost_below_zero(self):
        item = self.add_item(self.covered_offering, 100)
        self.add_item(
            self.covered_offering,
            -160,
            details={"is_discount": True, "discount_of_item": item.uuid.hex},
        )
        self.assertEqual(self.cost(), Decimal(0))

    def test_a_compensation_already_written_is_not_counted_as_cost(self):
        # A compensation is a draw made, not cost to draw against. The
        # compensation run clears them before recalculating and so never meets
        # one; reading an open month is not so protected.
        self.add_item(self.covered_offering, 100)
        self.add_item(
            self.covered_offering,
            -100,
            credit=self.customer_credit,
            details={"is_compensation": True},
        )
        self.assertEqual(self.cost(), Decimal(100))

    def test_none_when_this_month_has_no_invoice(self):
        # Distinct from zero, as for consumption_last_month: no billing period
        # is not the same statement as "nothing to draw".
        self.invoice.delete()
        self.assertIsNone(self.cost())

    def test_zero_when_the_organization_credit_is_gone(self):
        # Orphaned project credits predate the delete cascade. Without an
        # organization credit nothing defines which offerings are covered.
        self.add_item(self.covered_offering, 100)
        models.CustomerCredit.objects.filter(pk=self.customer_credit.pk).update(
            customer=structure_factories.CustomerFactory()
        )
        self.project_credit.refresh_from_db()
        self.assertEqual(self.cost(), 0)

    def test_the_field_is_served_to_project_members(self):
        # The pacing card renders for every project role, so this figure has
        # to survive the organization-scoped field stripping that removes
        # `offerings` and the organization totals beside it.
        self.add_item(self.covered_offering, 100)
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(
            factories.ProjectCreditFactory.get_url(self.project_credit)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["creditable_cost_this_month"]), 100)
        self.assertNotIn("offerings", response.data)
