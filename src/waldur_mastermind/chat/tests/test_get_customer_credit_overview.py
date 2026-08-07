from decimal import Decimal

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_customer_credit_overview import (
    GetCustomerCreditOverviewTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.models import Invoice
from waldur_mastermind.invoices.tests import factories as invoices_factories


class GetCustomerCreditOverviewToolTest(TestCase):
    def setUp(self):
        self.tool = GetCustomerCreditOverviewTool()
        self.fixture = ProjectFixture()
        self.customer = self.fixture.customer

    def _customer_credit(self, value="10000"):
        return invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal(value)
        )

    def _shared_invoice(self):
        # One invoice per customer/month — Invoice has unique_together
        # (customer, month, year), so all spend rows share it.
        invoice = Invoice.objects.filter(customer=self.customer).first()
        if invoice is None:
            invoice = invoices_factories.InvoiceFactory(customer=self.customer)
        return invoice

    def _project_with(self, name, credit_value, spend):
        # ProjectCredit.save() requires a CustomerCredit on the customer.
        project = structure_factories.ProjectFactory(customer=self.customer, name=name)
        invoices_factories.ProjectCreditFactory(
            project=project, value=Decimal(credit_value)
        )
        if spend:
            invoices_factories.InvoiceItemFactory(
                invoice=self._shared_invoice(),
                project=project,
                quantity=1,
                unit_price=Decimal(spend),
            )
        return project

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.GET_CUSTOMER_CREDIT_OVERVIEW, tool_registry)
        self.assertEqual(
            tool_registry.get(
                ToolName.GET_CUSTOMER_CREDIT_OVERVIEW
            ).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"customer_uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_unknown_customer_returns_error(self):
        result = self.tool.execute(
            self.fixture.staff, {"customer_name": "Nonexistent Org"}
        )
        self.assertEqual(result["type"], "error")

    def test_overview_rolls_up_projects_and_flags_overdrawn(self):
        self._customer_credit(value="10000")
        self._project_with("Over One", credit_value="100", spend="300")  # overdrawn
        self._project_with("Within One", credit_value="1000", spend="50")  # fine
        result = self.tool.execute(
            self.fixture.staff, {"customer_uuid": str(self.customer.uuid)}
        )
        self.assertEqual(result["type"], "success")
        data = result["data"]
        self.assertIsNotNone(data["customer_credit"])
        self.assertEqual(Decimal(data["customer_credit"]["value"]), Decimal("10000"))
        self.assertEqual(data["_total_project_count"], 2)
        self.assertEqual(data["overdrawn_count"], 1)
        # Worst (most overdrawn) sorts first.
        self.assertEqual(data["projects"][0]["project_name"], "Over One")
        self.assertTrue(data["projects"][0]["is_overdrawn"])
        self.assertIn("1 overdrawn", result["summary"])

    def test_no_customer_credit_branch(self):
        # Organization with no credit configured at all (the Crestford case).
        # ProjectCredit can't exist without a CustomerCredit, so "no customer
        # credit" implies no project credits either.
        result = self.tool.execute(
            self.fixture.staff, {"customer_uuid": str(self.customer.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertIsNone(result["data"]["customer_credit"])
        self.assertEqual(result["data"]["_total_project_count"], 0)
        self.assertIn("no customer credit configured", result["summary"].lower())

    def test_name_fallback(self):
        self.customer.name = "Acme Research Lab"
        self.customer.save()
        self._customer_credit()
        result = self.tool.execute(self.fixture.staff, {"customer_name": "acme"})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["customer"]["uuid"], str(self.customer.uuid))

    def test_project_only_member_sees_own_project_but_not_envelope(self):
        # CustomerCredit stays customer-role scoped, so the organization
        # envelope must not leak. ProjectCredit is readable by project roles,
        # but only for their own project — the sibling stays hidden, so the
        # rollup a project-only member gets is not an organization view.
        self._customer_credit(value="10000")
        invoices_factories.ProjectCreditFactory(
            project=self.fixture.project, value=Decimal("100")
        )
        invoices_factories.InvoiceItemFactory(
            invoice=self._shared_invoice(),
            project=self.fixture.project,
            quantity=1,
            unit_price=Decimal("300"),
        )
        self._project_with("Sibling", "1000", None)
        # Staff sees the envelope and both projects.
        staff_result = self.tool.execute(
            self.fixture.staff, {"customer_uuid": str(self.customer.uuid)}
        )
        self.assertIsNotNone(staff_result["data"]["customer_credit"])
        self.assertEqual(staff_result["data"]["_total_project_count"], 2)
        self.assertEqual(staff_result["data"]["overdrawn_count"], 1)
        # Project-only member: own project only, no organization credit.
        result = self.tool.execute(
            self.fixture.admin, {"customer_uuid": str(self.customer.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertIsNone(result["data"]["customer_credit"])
        self.assertEqual(result["data"]["_total_project_count"], 1)
        self.assertEqual(
            result["data"]["projects"][0]["project_uuid"],
            str(self.fixture.project.uuid),
        )
