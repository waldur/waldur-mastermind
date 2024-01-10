import datetime
import uuid
from unittest import mock

from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_core.core.utils import month_end, month_start, serialize_instance
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.invoices.models import Invoice, InvoiceItem
from waldur_mastermind.invoices.tests.factories import (
    InvoiceFactory,
    InvoiceItemFactory,
)
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.tasks import ResourceInvoicePullTask


class InvoiceItemPullTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("waldur_mastermind.marketplace_remote.utils.WaldurClient")
        self.client_mock = patcher.start()
        self.fixture = ProjectFixture()
        offering = OfferingFactory(
            type=PLUGIN_NAME,
            secret_options={
                "api_url": "https://remote-waldur.com/",
                "token": "valid_token",
                "customer_uuid": "customer-uuid",
            },
        )
        self.customer = self.fixture.customer
        self.resource = ResourceFactory(project=self.fixture.project, offering=offering)
        self.resource.backend_id = "valid-backend-id"
        self.resource.save()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def get_common_data(self, start=None, end=None, quantity=100):
        now = timezone.now()
        if start is None:
            start = month_start(now)
        if end is None:
            end = month_end(now)
        return {
            "unit": "sample-unit",
            "name": "Fake invoice item",
            "measured_unit": "sample-m-unit",
            "article_code": "",
            "unit_price": 2.0,
            "details": {},
            "quantity": quantity,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "uuid": uuid.uuid4().hex,
        }

    @freeze_time("2021-08-17")
    def test_invoice_is_created_after_pull(self):
        self.client_mock().list_invoice_items.return_value = []
        today = datetime.date.today()

        self.assertEqual(
            0,
            Invoice.objects.filter(
                customer__uuid=self.customer.uuid, year=today.year, month=today.month
            ).count(),
        )

        ResourceInvoicePullTask().run(serialize_instance(self.resource))
        self.assertEqual(
            1,
            Invoice.objects.filter(
                customer__uuid=self.customer.uuid, year=today.year, month=today.month
            ).count(),
        )

        ResourceInvoicePullTask().run(serialize_instance(self.resource))
        self.assertEqual(
            1,
            Invoice.objects.filter(
                customer__uuid=self.customer.uuid, year=today.year, month=today.month
            ).count(),
        )

    def test_invoice_items_creation(self):
        item_data = self.get_common_data()
        self.client_mock().list_invoice_items.return_value = [
            {"resource_uuid": self.resource.backend_id, **item_data}
        ]
        today = datetime.date.today()
        ResourceInvoicePullTask().run(serialize_instance(self.resource))
        invoice = Invoice.objects.get(
            customer__uuid=self.customer.uuid, year=today.year, month=today.month
        )
        self.assertEqual(
            1,
            InvoiceItem.objects.filter(
                resource__uuid=self.resource.uuid, invoice=invoice
            ).count(),
        )
        item = InvoiceItem.objects.get(
            resource__uuid=self.resource.uuid, invoice=invoice
        )
        self.assertEqual(2.0 * 100, item.total)

    def test_invoice_item_deletion(self):
        item_data = self.get_common_data()
        self.client_mock().list_invoice_items.return_value = []
        invoice = InvoiceFactory(customer=self.customer)
        InvoiceItemFactory(
            invoice=invoice,
            resource=self.resource,
            **item_data,
        )
        ResourceInvoicePullTask().run(serialize_instance(self.resource))
        self.assertEqual(
            0,
            InvoiceItem.objects.filter(
                resource__uuid=self.resource.uuid, invoice=invoice
            ).count(),
        )

    def test_invoice_item_modification(self):
        new_quantity = 200
        new_month_end = month_end(timezone.now() + datetime.timedelta(weeks=5))
        new_item_data = self.get_common_data(quantity=new_quantity, end=new_month_end)
        old_item_data = self.get_common_data()
        self.client_mock().list_invoice_items.return_value = [
            {"resource_uuid": self.resource.backend_id, **new_item_data}
        ]
        invoice = InvoiceFactory(customer=self.customer)
        item = InvoiceItemFactory(
            invoice=invoice,
            resource=self.resource,
            **old_item_data,
            backend_uuid=new_item_data["uuid"],
        )

        self.assertNotEqual(new_month_end, item.end)

        ResourceInvoicePullTask().run(serialize_instance(self.resource))

        item.refresh_from_db()
        self.assertEqual(new_quantity, item.quantity)
        self.assertEqual(new_month_end, item.end)
