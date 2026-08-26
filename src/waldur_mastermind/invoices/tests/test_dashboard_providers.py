import datetime

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests.factories import InvoiceFactory


class DashboardOverdueInvoiceProviderTest(test.APITestCase):
    def setUp(self):
        self.url = structure_factories.UserFactory.get_list_url(
            "dashboard-pending-actions"
        )
        # The provider scopes by the CUSTOMER.OWNER role itself — no extra
        # permission grant, mirroring the default role model.
        self.fixture = structure_fixtures.ProjectFixture()
        self.owner = self.fixture.owner

    def test_emits_single_aggregated_overdue_invoice_item(self):
        long_ago = (timezone.now() - datetime.timedelta(days=365)).date()
        # Overdue invoice in a prior year
        InvoiceFactory(
            customer=self.fixture.customer,
            state=invoice_models.Invoice.States.CREATED,
            invoice_date=long_ago,
            year=long_ago.year,
            month=long_ago.month,
        )
        # Non-overdue invoice (current month)
        today = timezone.now().date()
        InvoiceFactory(
            customer=self.fixture.customer,
            state=invoice_models.Invoice.States.CREATED,
            invoice_date=today,
            year=today.year,
            month=today.month,
        )
        # Paid invoice should not appear even when overdue (different month)
        prior_month = today.replace(day=1) - datetime.timedelta(days=1)
        InvoiceFactory(
            customer=self.fixture.customer,
            state=invoice_models.Invoice.States.PAID,
            invoice_date=long_ago,
            year=prior_month.year,
            month=prior_month.month,
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = [item for item in response.data if item["type"] == "invoice_overdue"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["variant"], "error")
        self.assertEqual(items[0]["count"], 1)
        # Single overdue invoice still carries the uuids the feed deep-links on.
        self.assertIsNotNone(items[0]["target_uuid"])
        self.assertIsNotNone(items[0]["customer_uuid"])

    def test_aggregates_many_overdue_invoices_into_one_item(self):
        # This feed is rebuilt on every dashboard load; one item per invoice
        # meant an owner with a backlog pushed dozens of rows into it.
        today = timezone.now().date()
        oldest = None
        for months in range(3, 9):
            invoice_date = today.replace(day=1) - datetime.timedelta(days=31 * months)
            invoice = InvoiceFactory(
                customer=self.fixture.customer,
                state=invoice_models.Invoice.States.CREATED,
                invoice_date=invoice_date,
                year=invoice_date.year,
                month=invoice_date.month,
            )
            if oldest is None or invoice_date < oldest.invoice_date:
                oldest = invoice

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        items = [item for item in response.data if item["type"] == "invoice_overdue"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 6)
        # Aggregated: no single invoice to deep-link to, and the deadline is
        # the oldest one's. All six share a customer, so that uuid survives and
        # the item can still link to the organization's invoice list.
        self.assertIsNone(items[0]["target_uuid"])
        self.assertEqual(items[0]["customer_uuid"], str(self.fixture.customer.uuid))
        self.assertEqual(items[0]["deadline"].date(), oldest.due_date)

    def test_drops_customer_uuid_when_overdue_invoices_span_organizations(self):
        other = structure_factories.CustomerFactory()
        other.add_user(self.owner, CustomerRole.OWNER)
        long_ago = (timezone.now() - datetime.timedelta(days=365)).date()
        for customer in (self.fixture.customer, other):
            InvoiceFactory(
                customer=customer,
                state=invoice_models.Invoice.States.CREATED,
                invoice_date=long_ago,
                year=long_ago.year,
                month=long_ago.month,
            )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        items = [item for item in response.data if item["type"] == "invoice_overdue"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 2)
        self.assertIsNone(items[0]["customer_uuid"])

    def test_non_owner_does_not_see_invoice_item(self):
        long_ago = (timezone.now() - datetime.timedelta(days=365)).date()
        InvoiceFactory(
            customer=self.fixture.customer,
            state=invoice_models.Invoice.States.CREATED,
            invoice_date=long_ago,
            year=long_ago.year,
            month=long_ago.month,
        )

        self.client.force_authenticate(self.fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        items = [item for item in response.data if item["type"] == "invoice_overdue"]
        self.assertEqual(items, [])
