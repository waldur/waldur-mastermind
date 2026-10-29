from decimal import Decimal
from uuid import UUID

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.list_invoices import ListInvoicesTool
from waldur_mastermind.chat.tools.base import MAX_LIST_RESULTS
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)
from waldur_mastermind.invoices.models import Invoice
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.invoices.utils import get_invoice_pdf_data

ROLES = ("owner", "member", "user", "global_support", "staff")


class ListInvoicesToolTest(APITestCase):
    def setUp(self):
        self.tool = ListInvoicesTool()
        self.fixture = ProjectFixture()
        self.march = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2026,
            month=3,
            state=Invoice.States.PAID,
            tax_percent=Decimal("10"),
        )
        invoices_factories.InvoiceItemFactory(
            invoice=self.march,
            project=self.fixture.project,
            unit_price=Decimal("100"),
            quantity=1,
        )
        self.april = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer, year=2026, month=4
        )
        self.other = invoices_factories.InvoiceFactory(year=2026, month=3)

    def _rest_uuids(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(
            invoices_factories.InvoiceFactory.get_list_url(),
            {"page_size": 100, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {UUID(str(i["uuid"])).hex for i in response.data}

    def _tool_result(self, user, **arguments):
        result = self.tool.execute(user, arguments)
        self.assertEqual(result["type"], "success")
        return result["data"]

    def _tool_uuids(self, user, **arguments):
        return {
            UUID(i["uuid"]).hex
            for i in self._tool_result(user, **arguments)["invoices"]
        }

    def test_registered_in_account_category_and_role_sets(self):
        self.assertEqual(
            tool_registry.get(ToolName.LIST_INVOICES).definition.category,
            ToolCategory.ACCOUNT,
        )
        for tool_set in (END_USER_TOOLS, SUPPORT_TOOLS, STAFF_TOOLS):
            self.assertIn(ToolName.LIST_INVOICES, tool_set)

    def test_visible_invoices_match_rest_list(self):
        for role in ROLES:
            with self.subTest(role=role):
                user = getattr(self.fixture, role)
                self.assertEqual(self._tool_uuids(user), self._rest_uuids(user))

    def test_project_member_and_unrelated_user_see_no_invoices(self):
        for user in (self.fixture.member, self.fixture.user):
            self.assertEqual(self._tool_uuids(user), set())
            result = self.tool.execute(user, {})
            self.assertIn("your role lets you see", result["summary"])

    def test_filters_match_rest(self):
        user = self.fixture.staff
        self.fixture.customer.name = "Bluewave Ledger"
        self.fixture.customer.save()
        cases = [
            (
                {"customer_uuid": str(self.fixture.customer.uuid)},
                {"customer_uuid": self.fixture.customer.uuid.hex},
            ),
            ({"year": 2026, "month": 3}, {"year": 2026, "month": 3}),
            ({"state": ["paid"]}, {"state": "paid"}),
            (
                {"customer_name": "bluewave"},
                {"customer_uuid": self.fixture.customer.uuid.hex},
            ),
        ]
        for arguments, params in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self._tool_uuids(user, **arguments),
                    self._rest_uuids(user, **params),
                )

    def test_totals_match_rest_as_strings(self):
        row = self._tool_result(self.fixture.owner, month=3)["invoices"][0]
        self.client.force_authenticate(self.fixture.owner)
        rest = self.client.get(
            invoices_factories.InvoiceFactory.get_url(self.march)
        ).data
        self.assertEqual(row["state"], "paid")
        self.assertIsInstance(row["total"], str)
        self.assertEqual(Decimal(row["price"]), Decimal(rest["price"]))
        self.assertEqual(Decimal(row["total"]), Decimal(rest["total"]))

    def test_tax_and_total_match_the_invoice_pdf(self):
        # 1.05 at 21% VAT is 0.2205: the PDF prints 0.22 and 1.27.
        invoice = invoices_factories.InvoiceFactory(
            customer=self.fixture.customer,
            year=2026,
            month=5,
            tax_percent=Decimal("21"),
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            unit_price=Decimal("1.05"),
            quantity=1,
        )
        row = self._tool_result(self.fixture.owner, month=5)["invoices"][0]
        self.assertEqual(
            (row["price"], row["tax"], row["total"]), ("1.05", "0.22", "1.27")
        )
        printed = [t["value"] for t in get_invoice_pdf_data(invoice)["totals"]]
        self.assertEqual(
            [value.split()[-1] for value in printed],
            [row["price"], row["tax"], row["total"]],
        )

    def test_non_string_state_is_rejected(self):
        for state in ([{"state": "paid"}], 4):
            with self.subTest(state=state):
                result = self.tool.execute(self.fixture.staff, {"state": state})
                self.assertEqual(result["type"], "validation_error")

    def test_invalid_period_is_rejected(self):
        for arguments in (
            {"month": 13},
            {"month": True},
            {"year": "last"},
            {"year": "\u00b2"},
            {"month": "\u00b3"},
        ):
            with self.subTest(arguments=arguments):
                result = self.tool.execute(self.fixture.staff, arguments)
                self.assertEqual(result["type"], "validation_error")

    def test_period_as_digit_strings_is_accepted(self):
        self.assertEqual(
            self._tool_uuids(self.fixture.staff, year="2026", month="3"),
            self._tool_uuids(self.fixture.staff, year=2026, month=3),
        )

    def test_results_are_capped(self):
        for year in range(2000, 2000 + MAX_LIST_RESULTS):
            invoices_factories.InvoiceFactory(
                customer=self.fixture.customer, year=year, month=1
            )
        data = self._tool_result(
            self.fixture.owner, customer_uuid=str(self.fixture.customer.uuid)
        )
        self.assertEqual(len(data["invoices"]), MAX_LIST_RESULTS)
        self.assertEqual(data["total"], MAX_LIST_RESULTS + 2)
        self.assertEqual(data["_total_count"], MAX_LIST_RESULTS + 2)
        self.assertTrue(data["_truncated"])
        self.assertEqual(data["invoices"][0]["uuid"], str(self.april.uuid))
