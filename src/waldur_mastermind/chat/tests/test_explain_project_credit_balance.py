from decimal import Decimal

from django.test import TestCase

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.explain_project_credit_balance import (
    ExplainProjectCreditBalanceTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.tests import factories as invoices_factories


class ExplainProjectCreditBalanceToolTest(TestCase):
    def setUp(self):
        self.tool = ExplainProjectCreditBalanceTool()
        self.fixture = ProjectFixture()

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE, tool_registry)
        self.assertEqual(
            tool_registry.get(
                ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE
            ).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"project_uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_unknown_project_returns_error(self):
        result = self.tool.execute(
            self.fixture.staff, {"project_name": "Nonexistent Phantom"}
        )
        self.assertEqual(result["type"], "error")
        self.assertIn("not found", result["summary"].lower())

    def test_no_credit_configured_branch(self):
        # Project exists but no ProjectCredit / CustomerCredit row.
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        self.assertEqual(result["type"], "success")
        self.assertIsNone(result["data"]["project_credit"])
        self.assertIsNone(result["data"]["customer_credit"])
        self.assertIn("no project credit configured", result["summary"].lower())

    def test_overdrawn_when_spend_exceeds_value(self):
        invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("10000")
        )
        invoices_factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("100")
        )
        invoice = invoices_factories.InvoiceFactory(customer=self.fixture.customer)
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            quantity=10,
            unit_price=Decimal("20"),  # 10 × 20 = 200 > 100 → overdrawn
        )
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        self.assertEqual(result["type"], "success")
        self.assertTrue(result["data"]["project_credit"]["is_overdrawn"])
        self.assertIn("OVERDRAWN", result["summary"])

    def test_within_budget_when_spend_below_value(self):
        invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("10000")
        )
        invoices_factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("1000")
        )
        invoice = invoices_factories.InvoiceFactory(customer=self.fixture.customer)
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            quantity=5,
            unit_price=Decimal("10"),  # 50 < 1000
        )
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        self.assertEqual(result["type"], "success")
        self.assertFalse(result["data"]["project_credit"]["is_overdrawn"])
        self.assertEqual(result["data"]["project_credit"]["spent_to_date"], "50.0000")

    def test_includes_customer_credit_envelope(self):
        # CustomerCredit must exist before ProjectCredit
        # (ProjectCredit.save() validates this).
        invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer,
            value=Decimal("10000"),
            expected_consumption=Decimal("8000"),
        )
        invoices_factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("500")
        )
        result = self.tool.execute(
            self.fixture.staff,
            {"project_uuid": str(self.fixture.project.uuid)},
        )
        cc = result["data"]["customer_credit"]
        self.assertIsNotNone(cc)
        # value is rendered via str(Decimal), e.g. "10000.00000".
        self.assertEqual(Decimal(cc["value"]), Decimal("10000"))
        self.assertEqual(Decimal(cc["expected_consumption"]), Decimal("8000"))

    def test_name_fallback(self):
        self.fixture.project.name = "Project Yarrow"
        self.fixture.project.save()
        result = self.tool.execute(self.fixture.staff, {"project_name": "yarrow"})
        self.assertEqual(result["type"], "success")
        self.assertEqual(
            result["data"]["project"]["uuid"], str(self.fixture.project.uuid)
        )

    def test_inaccessible_project_returns_not_found(self):
        # A different fixture's project is invisible to this fixture's manager.
        other = ProjectFixture()
        result = self.tool.execute(
            self.fixture.manager,
            {"project_uuid": str(other.project.uuid)},
        )
        self.assertEqual(result["type"], "error")

    def test_project_member_without_customer_role_denied_financials(self):
        # Credit / invoice data is customer-role scoped. A project-only member
        # can resolve the project but must NOT see its credit balance, the
        # customer envelope, or spend totals (matches the REST boundary).
        invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("10000")
        )
        invoices_factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("100")
        )
        invoice = invoices_factories.InvoiceFactory(customer=self.fixture.customer)
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            quantity=10,
            unit_price=Decimal("20"),  # 200 > 100 → overdrawn for an authorized viewer
        )
        # Staff (and customer owner) see the overdrawn credit + envelope.
        staff_result = self.tool.execute(
            self.fixture.staff, {"project_uuid": str(self.fixture.project.uuid)}
        )
        self.assertTrue(staff_result["data"]["project_credit"]["is_overdrawn"])
        self.assertIsNotNone(staff_result["data"]["customer_credit"])
        # Project-only member: project resolves, but no financial data leaks.
        result = self.tool.execute(
            self.fixture.admin, {"project_uuid": str(self.fixture.project.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertIsNone(result["data"]["project_credit"])
        self.assertIsNone(result["data"]["customer_credit"])
        self.assertIn("no project credit configured", result["summary"].lower())
