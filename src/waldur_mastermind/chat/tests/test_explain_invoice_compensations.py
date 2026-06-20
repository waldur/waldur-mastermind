from decimal import Decimal

from django.test import TestCase

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.explain_invoice_compensations import (
    ExplainInvoiceCompensationsTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ExplainInvoiceCompensationsToolTest(TestCase):
    def setUp(self):
        self.tool = ExplainInvoiceCompensationsTool()
        self.fixture = ProjectFixture()
        self.customer_credit = invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("10000")
        )
        self.invoice = invoices_factories.InvoiceFactory(customer=self.fixture.customer)

    def _make_resource(self, name="Resource 1", state=ResourceStates.OK):
        return marketplace_factories.ResourceFactory(
            project=self.fixture.project, name=name, state=state
        )

    def _add_charge(self, resource, unit_price, quantity=1):
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=resource,
            project=self.fixture.project,
            quantity=quantity,
            unit_price=Decimal(str(unit_price)),
        )

    def _add_compensation(self, resource, amount):
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=resource,
            project=self.fixture.project,
            quantity=1,
            unit_price=Decimal(str(-amount)),
            credit=self.customer_credit,
            name=f"Credit compensation. {resource.name}",
        )

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.EXPLAIN_INVOICE_COMPENSATIONS, tool_registry)
        self.assertEqual(
            tool_registry.get(
                ToolName.EXPLAIN_INVOICE_COMPENSATIONS
            ).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_unknown_project_returns_error(self):
        result = self.tool.execute(self.fixture.staff, {"project_name": "Nonexistent"})
        self.assertEqual(result["type"], "error")

    def test_no_invoice_items_returns_empty_summary(self):
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        self.assertEqual(result["type"], "success")
        self.assertIsNone(result["data"]["invoice_summary"])
        self.assertIn("no invoice items", result["summary"].lower())

    def test_gross_only_no_compensation(self):
        r = self._make_resource()
        self._add_charge(r, 100, 1)
        self._add_charge(r, 50, 1)
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        s = result["data"]["invoice_summary"]
        self.assertEqual(Decimal(s["gross"]), Decimal("150"))
        self.assertEqual(Decimal(s["compensation_total"]), Decimal("0"))
        self.assertEqual(Decimal(s["net_charged_to_customer"]), Decimal("150"))

    def test_compensation_split_from_gross(self):
        r = self._make_resource()
        self._add_charge(r, 200, 1)
        self._add_compensation(r, 80)  # credit-compensated
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        s = result["data"]["invoice_summary"]
        self.assertEqual(Decimal(s["gross"]), Decimal("200"))
        self.assertEqual(Decimal(s["compensation_total"]), Decimal("80"))
        self.assertEqual(Decimal(s["from_customer_credit"]), Decimal("80"))
        self.assertEqual(Decimal(s["net_charged_to_customer"]), Decimal("120"))
        # from_project_credit was always "0" (compensation FK only points at
        # CustomerCredit) — the misleading field must not be surfaced.
        self.assertNotIn("from_project_credit", s)

    def test_terminated_resource_flagged_concealed(self):
        live = self._make_resource("Live VM", state=ResourceStates.OK)
        gone = self._make_resource("Dead VM", state=ResourceStates.TERMINATED)
        self._add_charge(live, 100)
        self._add_charge(gone, 50)
        self._add_compensation(gone, 50)  # fully compensated
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        self.assertEqual(result["data"]["concealed_resource_count"], 1)
        rows = {row["resource_name"]: row for row in result["data"]["per_resource"]}
        self.assertTrue(rows["Dead VM"]["hidden_from_user"])
        self.assertFalse(rows["Live VM"]["hidden_from_user"])
        self.assertEqual(Decimal(rows["Dead VM"]["compensation"]), Decimal("50"))
        self.assertEqual(Decimal(rows["Dead VM"]["net"]), Decimal("0"))

    def test_manual_refund_separate_from_compensation(self):
        r = self._make_resource()
        self._add_charge(r, 200)
        # Manual refund: negative price, NO credit FK.
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            resource=r,
            project=self.fixture.project,
            quantity=1,
            unit_price=Decimal("-30"),
            name="Manual cost correction",
        )
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        s = result["data"]["invoice_summary"]
        self.assertEqual(Decimal(s["gross"]), Decimal("200"))
        self.assertEqual(Decimal(s["compensation_total"]), Decimal("0"))
        self.assertEqual(Decimal(s["manual_refunds_total"]), Decimal("30"))
        self.assertEqual(Decimal(s["net_charged_to_customer"]), Decimal("170"))
        self.assertEqual(len(result["data"]["manual_adjustments"]), 1)
        self.assertEqual(
            result["data"]["manual_adjustments"][0]["name"], "Manual cost correction"
        )

    def test_year_month_filter(self):
        r = self._make_resource()
        self._add_charge(r, 100)
        # Latest invoice picks the only month available — explicit filter
        # for a different month must yield empty.
        result = self.tool.execute(
            self.fixture.staff,
            {
                "project_uuid": str(self.fixture.project.uuid),
                "year": 2020,
                "month": 1,
            },
        )
        s = result["data"]["invoice_summary"]
        self.assertEqual(s["year"], 2020)
        self.assertEqual(s["month"], 1)
        self.assertEqual(Decimal(s["gross"]), Decimal("0"))

    def test_inaccessible_project_denied(self):
        other = ProjectFixture()
        result = self.tool.execute(
            other.member, {"project_uuid": str(self.fixture.project.uuid)}
        )
        self.assertEqual(result["type"], "error")

    def test_project_member_without_customer_role_denied_items(self):
        # InvoiceItem is customer-role scoped. A project-only member resolves
        # the project but must see no invoice activity — no compensation or
        # refund line items leak through project membership.
        r = self._make_resource()
        self._add_charge(r, 200, 1)
        self._add_compensation(r, 80)
        # Staff sees the invoice summary...
        staff_result = self.tool.execute(
            self.fixture.staff, {"project_uuid": str(self.fixture.project.uuid)}
        )
        self.assertIsNotNone(staff_result["data"]["invoice_summary"])
        # ...but a project-only member (no customer role) sees nothing.
        result = self.tool.execute(
            self.fixture.admin, {"project_uuid": str(self.fixture.project.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertIsNone(result["data"]["invoice_summary"])
        self.assertIn("no invoice items", result["summary"].lower())
