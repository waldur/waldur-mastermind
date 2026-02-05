from decimal import Decimal

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices import models
from waldur_mastermind.invoices.tests import factories, fixtures


def get_import_usage_url():
    return factories.InvoiceFactory.get_list_url() + "import_usage/"


@ddt
class ImportUsagePermissionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = get_import_usage_url()
        self.valid_payload = {
            "year": 2024,
            "month": 1,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Test Service",
                    "unit_price": "10.00",
                }
            ],
        }

    def test_staff_can_import_usage(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "manager", "admin", "user")
    def test_non_staff_cannot_import_usage(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_cannot_import_usage(self):
        response = self.client.post(self.url, self.valid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ImportUsageSuccessTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = get_import_usage_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_successful_import_creates_invoice_item(self):
        payload = {
            "year": 2024,
            "month": 6,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Cloud Storage",
                    "unit_price": "25.50",
                    "article_code": "STORAGE-001",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(response.data["skipped"], 0)
        self.assertEqual(response.data["errors"], [])

        # Verify invoice item was created
        invoice = models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=6
        )
        item = invoice.items.get(name="Cloud Storage")
        self.assertEqual(item.unit_price, Decimal("25.50"))
        self.assertEqual(item.article_code, "STORAGE-001")
        self.assertEqual(item.quantity, 1)

    def test_import_with_customer_uuid(self):
        payload = {
            "year": 2024,
            "month": 7,
            "items": [
                {
                    "customer_uuid": str(self.fixture.customer.uuid),
                    "name": "Compute Service",
                    "unit_price": "100.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 1)

    def test_import_stores_metadata_in_details(self):
        payload = {
            "year": 2024,
            "month": 8,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "VM Instance",
                    "unit_price": "50.00",
                    "service_provider_name": "Cloud Provider X",
                    "offering_name": "Standard VM",
                    "plan_name": "Monthly Plan",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invoice = models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=8
        )
        item = invoice.items.get(name="VM Instance")
        self.assertEqual(item.details["service_provider_name"], "Cloud Provider X")
        self.assertEqual(item.details["offering_name"], "Standard VM")
        self.assertEqual(item.details["plan_name"], "Monthly Plan")

    def test_import_multiple_items(self):
        customer2 = structure_factories.CustomerFactory()
        payload = {
            "year": 2024,
            "month": 9,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service A",
                    "unit_price": "10.00",
                },
                {
                    "customer_name": customer2.name,
                    "name": "Service B",
                    "unit_price": "20.00",
                },
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 2)


class ImportUsageSkippingTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = get_import_usage_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_zero_amount_items_are_skipped(self):
        payload = {
            "year": 2024,
            "month": 10,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Free Service",
                    "unit_price": "0.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(response.data["skipped"], 1)

    def test_duplicate_items_are_skipped(self):
        # First import
        payload = {
            "year": 2024,
            "month": 11,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Recurring Service",
                    "unit_price": "15.00",
                    "article_code": "REC-001",
                }
            ],
        }

        response1 = self.client.post(self.url, payload, format="json")
        self.assertEqual(response1.data["created"], 1)

        # Second import with same item
        response2 = self.client.post(self.url, payload, format="json")
        self.assertEqual(response2.data["created"], 0)
        self.assertEqual(response2.data["skipped"], 1)

        # Verify only one item exists
        invoice = models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=11
        )
        self.assertEqual(invoice.items.count(), 1)

    def test_different_article_codes_create_separate_items(self):
        payload1 = {
            "year": 2024,
            "month": 12,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service X",
                    "unit_price": "10.00",
                    "article_code": "CODE-A",
                }
            ],
        }
        payload2 = {
            "year": 2024,
            "month": 12,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service X",
                    "unit_price": "10.00",
                    "article_code": "CODE-B",
                }
            ],
        }

        self.client.post(self.url, payload1, format="json")
        response = self.client.post(self.url, payload2, format="json")

        self.assertEqual(response.data["created"], 1)
        invoice = models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=12
        )
        self.assertEqual(invoice.items.count(), 2)


class ImportUsageErrorTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = get_import_usage_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_customer_not_found_by_name(self):
        payload = {
            "year": 2024,
            "month": 1,
            "items": [
                {
                    "customer_name": "Non-existent Customer",
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(len(response.data["errors"]), 1)
        self.assertEqual(
            response.data["errors"][0]["customer_name"], "Non-existent Customer"
        )
        self.assertEqual(response.data["errors"][0]["reason"], "Customer not found")

    def test_customer_not_found_by_uuid(self):
        import uuid

        payload = {
            "year": 2024,
            "month": 1,
            "items": [
                {
                    "customer_uuid": str(uuid.uuid4()),
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(len(response.data["errors"]), 1)
        self.assertEqual(response.data["errors"][0]["reason"], "Customer not found")

    def test_non_pending_invoice_rejected(self):
        # Create an existing invoice with CREATED state
        factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2024,
            month=2,
            state=models.Invoice.States.CREATED,
        )

        payload = {
            "year": 2024,
            "month": 2,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(len(response.data["errors"]), 1)
        self.assertIn("created", response.data["errors"][0]["reason"])
        self.assertIn("mutable", response.data["errors"][0]["reason"])

    def test_paid_invoice_rejected(self):
        factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2024,
            month=3,
            state=models.Invoice.States.PAID,
        )

        payload = {
            "year": 2024,
            "month": 3,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(len(response.data["errors"]), 1)

    def test_canceled_invoice_rejected(self):
        factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2024,
            month=4,
            state=models.Invoice.States.CANCELED,
        )

        payload = {
            "year": 2024,
            "month": 4,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(len(response.data["errors"]), 1)


class ImportUsageValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.InvoiceFixture()
        self.url = get_import_usage_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_empty_items_rejected(self):
        payload = {
            "year": 2024,
            "month": 1,
            "items": [],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_customer_identifier_rejected(self):
        payload = {
            "year": 2024,
            "month": 1,
            "items": [
                {
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_year_rejected(self):
        payload = {
            "year": 1999,
            "month": 1,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_month_rejected(self):
        payload = {
            "year": 2024,
            "month": 13,
            "items": [
                {
                    "customer_name": self.fixture.customer.name,
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_name_lookup_is_case_insensitive(self):
        payload = {
            "year": 2024,
            "month": 5,
            "items": [
                {
                    "customer_name": self.fixture.customer.name.upper(),
                    "name": "Service",
                    "unit_price": "10.00",
                }
            ],
        }

        response = self.client.post(self.url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 1)
