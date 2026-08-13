"""Project credits are removed with the organization credit that funds them.

A project credit is an allocation out of the organization credit: it can only
be created while one exists and can draw nothing without it. Deleting the
organization credit now takes them with it.

The reader also stays defensive. Orphans created before the cascade existed are
still out there, and `consumption_last_month` used `.get()` on the organization
credit, so evaluating it raised. The API itself is not exposed to that —
`ProjectCreditViewSet.queryset` excludes credits whose customer has none, so an
orphan 404s rather than erroring — but shells, tasks and any future caller that
does not inherit that queryset are.
"""

from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.tests import factories, fixtures


@freeze_time("2024-02-05")
class ProjectCreditWithoutCustomerCreditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CreditFixture()
        self.customer_credit = self.fixture.customer_credit
        self.project_credit = self.fixture.project_credit
        # The month `consumption_last_month` looks at.
        self.invoice = factories.InvoiceFactory(
            customer=self.fixture.customer, year=2024, month=1
        )
        self.url = factories.ProjectCreditFactory.get_url(self.project_credit)

    def orphan(self):
        """Reproduce a pre-cascade orphan: drop the row without the signal."""
        models.CustomerCredit.objects.filter(pk=self.customer_credit.pk).update(
            customer=structure_factories.CustomerFactory()
        )

    def test_property_returns_zero_when_organization_credit_is_gone(self):
        self.orphan()
        self.project_credit.refresh_from_db()
        self.assertEqual(self.project_credit.consumption_last_month, 0)

    def test_api_hides_an_orphaned_project_credit(self):
        # Documents existing behaviour rather than asserting a fix: the viewset
        # excludes credits whose customer has none, so an orphan disappears
        # from the API instead of erroring.
        self.orphan()
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
        listing = self.client.get(factories.ProjectCreditFactory.get_list_url())
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertNotIn(
            self.project_credit.uuid.hex, [c["uuid"] for c in listing.data]
        )

    def test_none_when_the_previous_month_has_no_invoice(self):
        # Distinct from zero: no billing period is not the same statement as
        # "drew nothing", and the serialised field keeps them apart.
        self.invoice.delete()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["consumption_last_month"])

    def test_consumption_is_reported_when_credit_was_drawn(self):
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.fixture.project,
            credit=self.customer_credit,
            unit_price=-40,
            quantity=1,
        )
        self.project_credit.refresh_from_db()
        self.assertEqual(self.project_credit.consumption_last_month, 40)

    def test_other_projects_credit_is_not_counted(self):
        other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=other_project,
            credit=self.customer_credit,
            unit_price=-40,
            quantity=1,
        )
        self.project_credit.refresh_from_db()
        self.assertEqual(self.project_credit.consumption_last_month, 0)

    def test_deleting_the_organization_credit_removes_project_credits(self):
        other_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        other_credit = factories.ProjectCreditFactory(project=other_project)
        # A different organization, with its own credit — ProjectCredit.save()
        # refuses to create an allocation without one.
        unrelated_project = structure_factories.ProjectFactory()
        factories.CustomerCreditFactory(customer=unrelated_project.customer)
        unrelated = factories.ProjectCreditFactory(project=unrelated_project)

        self.customer_credit.delete()

        self.assertFalse(
            models.ProjectCredit.objects.filter(pk=self.project_credit.pk).exists()
        )
        self.assertFalse(
            models.ProjectCredit.objects.filter(pk=other_credit.pk).exists()
        )
        # A different organization is untouched.
        self.assertTrue(models.ProjectCredit.objects.filter(pk=unrelated.pk).exists())
