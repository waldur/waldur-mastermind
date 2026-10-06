import datetime
import io
from decimal import Decimal
from unittest import mock

import pypdf
from constance.test import override_config
from django.core import mail
from django.test import TestCase, override_settings
from freezegun import freeze_time

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models, tasks, utils
from waldur_mastermind.invoices.tests import factories

ISSUER_DETAILS = {
    "company": "Issuer Ltd",
    "address": "Main street 1",
    "country": "Estonia",
    "postal": "10111",
    "phone": {"country_code": "372", "national_number": "5555555"},
    "bank": "Bank",
    "account": "EE001",
    "vat_code": "EE100",
    "email": "billing@example.com",
}


@override_config(CURRENCY_NAME="EUR")
@override_settings(
    WALDUR_INVOICES={"ISSUER_DETAILS": ISSUER_DETAILS, "PAYMENT_INTERVAL": 30}
)
@freeze_time("2026-10-01")
class InvoicePdfTest(TestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory(
            name="Customer & Co", vat_code="EE200", country="EE", postal="20222"
        )
        self.invoice = factories.InvoiceFactory(
            customer=self.customer,
            year=2026,
            month=9,
            invoice_date=datetime.date(2026, 10, 1),
            tax_percent=24,
        )
        self.project = structure_factories.ProjectFactory(
            customer=self.customer, name="Project A"
        )

    def pdf_pages(self):
        reader = pypdf.PdfReader(io.BytesIO(utils.create_invoice_pdf(self.invoice)))
        return [page.extract_text() for page in reader.pages]

    def items(self, data, project=0):
        return data["projects"][project]["items"]

    def totals(self, data):
        return [(row["label"], row["value"]) for row in data["totals"]]

    def add_item(self, name, quantity, unit_price, project=None):
        return factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=project or self.project,
            project_name=(project or self.project).name,
            name=name,
            quantity=Decimal(quantity),
            unit_price=Decimal(unit_price),
            unit=models.InvoiceItem.Units.QUANTITY,
            start=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
            end=datetime.datetime(2026, 9, 30, 23, 59, tzinfo=datetime.UTC),
        )

    def test_data_contains_invoice_parties_items_and_totals(self):
        self.add_item("Compute", "30", "1234.5")
        data = utils.get_invoice_pdf_data(self.invoice)

        self.assertEqual(data["title"], f"Invoice No. {self.invoice.number}")
        self.assertIn({"label": "Invoice date", "value": "2026-10-01"}, data["facts"])
        self.assertIn({"label": "Due date", "value": "2026-10-31"}, data["facts"])
        self.assertIn({"label": "Invoice period", "value": "2026-09"}, data["facts"])

        self.assertEqual(data["issuer"][0], "Issuer Ltd")
        self.assertIn("(372) 5555555", data["issuer"])
        self.assertIn("VAT: EE100", data["issuer"])
        self.assertEqual(data["customer"][0], "Customer & Co")
        self.assertIn("VAT: EE200", data["customer"])

        [item] = self.items(data)
        self.assertEqual(data["projects"][0]["name"], "Project A")
        self.assertEqual(item["name"], "Compute")
        self.assertEqual(item["quantity"], "30")
        self.assertEqual(item["unit_price"], "EUR 1,234.50")
        self.assertEqual(item["price"], "EUR 37,035.00")
        self.assertEqual(
            self.totals(data),
            [
                ("Subtotal", "EUR 37,035.00"),
                ("VAT", "EUR 8,888.40"),
                ("TOTAL", "EUR 45,923.40"),
            ],
        )

    def test_sub_cent_unit_price_and_large_quantity_keep_their_precision(self):
        self.add_item("CPU core-hours", "18400", "0.012")
        [item] = self.items(utils.get_invoice_pdf_data(self.invoice))
        self.assertEqual(item["quantity"], "18,400")
        self.assertEqual(item["unit_price"], "EUR 0.012")
        self.assertEqual(item["price"], "EUR 220.80")

    def test_subtotal_is_the_sum_of_the_printed_rows(self):
        # Item prices are rounded up to the cent per item, so 0.004 prints 0.01
        for name in ("A", "B", "C"):
            self.add_item(name, "1", "0.004")
        self.add_item("D", "1", "10.001")
        data = utils.get_invoice_pdf_data(self.invoice)
        self.assertEqual(
            [item["price"] for item in self.items(data)],
            ["EUR 0.01", "EUR 0.01", "EUR 0.01", "EUR 10.01"],
        )
        self.assertEqual(self.totals(data)[0], ("Subtotal", "EUR 10.04"))
        self.assertEqual(self.invoice.price, Decimal("10.04"))

    def test_total_is_the_sum_of_printed_subtotal_and_vat(self):
        # 15% of 16.10 is exactly half a cent over 2.41: VAT rounds half-up,
        # and TOTAL is the printed subtotal plus the printed VAT.
        self.add_item("Compute", "1", "16.10")
        self.invoice.tax_percent = Decimal("15")
        self.invoice.save()
        data = utils.get_invoice_pdf_data(self.invoice)
        self.assertEqual(
            self.totals(data),
            [("Subtotal", "EUR 16.10"), ("VAT", "EUR 2.42"), ("TOTAL", "EUR 18.52")],
        )

    def test_items_with_zero_price_are_omitted(self):
        self.add_item("Billed", "1", "10")
        self.add_item("Free", "1", "0")
        names = [
            item["name"]
            for item in self.items(utils.get_invoice_pdf_data(self.invoice))
        ]
        self.assertEqual(names, ["Billed"])

    def test_items_are_grouped_by_project_and_sorted_by_name(self):
        other = structure_factories.ProjectFactory(
            customer=self.customer, name="Project B"
        )
        self.add_item("Storage", "1", "5", project=other)
        self.add_item("Network", "1", "1")
        self.add_item("Compute", "1", "10")
        data = utils.get_invoice_pdf_data(self.invoice)
        self.assertEqual(
            [
                (project["name"], [item["name"] for item in project["items"]])
                for project in data["projects"]
            ],
            [("Project A", ["Compute", "Network"]), ("Project B", ["Storage"])],
        )

    def test_projects_with_the_same_name_are_not_merged(self):
        twin = structure_factories.ProjectFactory(
            customer=self.customer, name="Project A"
        )
        self.add_item("Compute", "1", "10")
        self.add_item("Storage", "1", "5", project=twin)
        data = utils.get_invoice_pdf_data(self.invoice)
        self.assertEqual(
            [project["name"] for project in data["projects"]],
            ["Project A", "Project A"],
        )

    def test_vat_row_is_omitted_without_tax(self):
        self.invoice.tax_percent = 0
        self.invoice.save()
        self.add_item("Compute", "1", "10")
        data = utils.get_invoice_pdf_data(self.invoice)
        self.assertEqual(
            [label for label, _ in self.totals(data)], ["Subtotal", "TOTAL"]
        )

    def test_pending_invoice_has_no_due_date(self):
        self.invoice.invoice_date = None
        self.invoice.save()
        data = utils.get_invoice_pdf_data(self.invoice)
        self.assertEqual(
            data["facts"][0], {"label": "Invoice date", "value": "Pending"}
        )
        self.assertNotIn("Due date", [fact["label"] for fact in data["facts"]])

    def test_pdf_contains_the_invoice(self):
        self.add_item("Compute", "1", "10")
        [text] = self.pdf_pages()
        self.assertIn(f"Invoice No. {self.invoice.number}", text)
        self.assertIn("Customer & Co", text)
        self.assertIn("EUR 12.40", text)

    def test_markup_in_names_is_printed_literally(self):
        self.customer.name = 'Lab #panic("customer") *bold* $x$'
        self.customer.save()
        self.add_item("VM _one_ <lbl> @ref", "1", "10")
        [text] = self.pdf_pages()
        self.assertIn('Lab #panic("customer") *bold* $x$', text)
        self.assertIn("VM _one_ <lbl> @ref", text)

    def test_non_latin_names_are_rendered(self):
        self.customer.name = "Организация Õnne"
        self.customer.save()
        self.add_item("Вычисления", "1", "10")
        [text] = self.pdf_pages()
        self.assertIn("Организация Õnne", text)
        self.assertIn("Вычисления", text)

    def test_long_invoice_spans_several_pages(self):
        for index in range(120):
            self.add_item(f"Item {index:03d}", "1", "10")
        pages = self.pdf_pages()
        self.assertGreater(len(pages), 1)
        # the table header is repeated on every page
        for text in pages:
            self.assertIn("Unit price", text)


@override_settings(task_always_eager=True)
class InvoicePdfFailureTest(TestCase):
    def setUp(self):
        fixture = structure_fixtures.CustomerFixture()
        fixture.owner
        self.invoice = factories.InvoiceFactory(customer=fixture.customer)
        structure_factories.NotificationFactory(key="invoices.notification")

    @mock.patch(
        "waldur_mastermind.invoices.utils.create_invoice_pdf",
        side_effect=RuntimeError("boom"),
    )
    def test_notification_is_sent_without_attachment_if_pdf_fails(self, _render):
        tasks.send_invoice_notification(self.invoice.uuid.hex)
        [message] = mail.outbox
        self.assertEqual(message.attachments, [])
        self.assertIn("You can view it online", message.body)
        self.assertNotIn("attached", message.body)
