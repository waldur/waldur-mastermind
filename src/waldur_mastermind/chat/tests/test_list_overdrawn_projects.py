from decimal import Decimal

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.list_overdrawn_projects import (
    ListOverdrawnProjectsTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.tests import factories as invoices_factories


class ListOverdrawnProjectsToolTest(TestCase):
    def setUp(self):
        self.tool = ListOverdrawnProjectsTool()

    def _ensure_customer_credit(self, customer):
        from waldur_mastermind.invoices.models import CustomerCredit, Invoice

        if not CustomerCredit.objects.filter(customer=customer).exists():
            invoices_factories.CustomerCreditFactory(
                customer=customer, value=Decimal("10000")
            )
        # Reuse a single Invoice per customer/month to avoid the
        # (customer, year, month) unique constraint when adding several
        # projects with spend in the same test.
        invoice = Invoice.objects.filter(customer=customer).first()
        if invoice is None:
            invoice = invoices_factories.InvoiceFactory(customer=customer)
        return invoice

    def _add_project_with_spend(self, customer, name, credit_value, spend_amount):
        invoice = self._ensure_customer_credit(customer)
        project = structure_factories.ProjectFactory(customer=customer, name=name)
        invoices_factories.ProjectCreditFactory(
            project=project, value=Decimal(credit_value)
        )
        if spend_amount:
            invoices_factories.InvoiceItemFactory(
                invoice=invoice,
                project=project,
                quantity=1,
                unit_price=Decimal(spend_amount),
            )
        return project

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.LIST_OVERDRAWN_PROJECTS, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.LIST_OVERDRAWN_PROJECTS).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_returns_empty_when_no_overdrawn(self):
        fixture = ProjectFixture()
        self._ensure_customer_credit(fixture.customer)
        invoices_factories.ProjectCreditFactory(
            project=fixture.project, value=Decimal("1000")
        )
        # No invoice items → no spend → not overdrawn.
        result = self.tool.execute(fixture.staff, {})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["_total_count"], 0)
        self.assertIn("No overdrawn", result["summary"])

    def test_finds_overdrawn_projects(self):
        fixture = ProjectFixture()
        # In-budget project (excluded from result)
        self._add_project_with_spend(fixture.customer, "Within Budget", "1000", "50")
        # Overdrawn project
        self._add_project_with_spend(fixture.customer, "Over Spent Alpha", "100", "300")
        # Severely overdrawn project
        self._add_project_with_spend(
            fixture.customer, "Severely Over Beta", "100", "500"
        )

        result = self.tool.execute(fixture.staff, {})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["_total_count"], 2)
        names = [p["project_name"] for p in result["data"]["overdrawn_projects"]]
        # Sorted by exceeded_by desc → severely over comes first.
        self.assertEqual(names, ["Severely Over Beta", "Over Spent Alpha"])

    def test_spend_grouped_per_project_no_bleed(self):
        # Two projects on one shared invoice, each with several items. The
        # bulk-fetch groups spend by project_id; assert one project's items
        # don't leak into the other's total.
        fixture = ProjectFixture()
        invoice = self._ensure_customer_credit(fixture.customer)
        over = structure_factories.ProjectFactory(
            customer=fixture.customer, name="Over Multi"
        )
        under = structure_factories.ProjectFactory(
            customer=fixture.customer, name="Under Multi"
        )
        invoices_factories.ProjectCreditFactory(project=over, value=Decimal("100"))
        invoices_factories.ProjectCreditFactory(project=under, value=Decimal("100"))
        # Over: 60 + 60 = 120 > 100 (overdrawn). Under: 20 + 20 = 40 < 100.
        for amount in ("60", "60"):
            invoices_factories.InvoiceItemFactory(
                invoice=invoice, project=over, quantity=1, unit_price=Decimal(amount)
            )
        for amount in ("20", "20"):
            invoices_factories.InvoiceItemFactory(
                invoice=invoice, project=under, quantity=1, unit_price=Decimal(amount)
            )

        result = self.tool.execute(fixture.staff, {})
        rows = {r["project_name"]: r for r in result["data"]["overdrawn_projects"]}
        self.assertEqual(result["data"]["_total_count"], 1)
        self.assertIn("Over Multi", rows)
        self.assertNotIn("Under Multi", rows)
        self.assertEqual(Decimal(rows["Over Multi"]["spent_to_date"]), Decimal("120"))
        self.assertEqual(Decimal(rows["Over Multi"]["exceeded_by"]), Decimal("20"))

    def test_customer_name_filter(self):
        fixture = ProjectFixture()
        other_customer = structure_factories.CustomerFactory(name="Other Org")

        self._add_project_with_spend(fixture.customer, "Mine Over", "100", "300")
        self._add_project_with_spend(other_customer, "Other Over", "100", "500")

        result = self.tool.execute(
            fixture.staff, {"customer_name": fixture.customer.name}
        )
        self.assertEqual(result["data"]["_total_count"], 1)
        self.assertEqual(
            result["data"]["overdrawn_projects"][0]["project_name"], "Mine Over"
        )

    def test_project_only_member_denied_credit_visibility(self):
        # ProjectCredit is customer-role scoped. A project-level member with no
        # customer role must NOT see overdrawn credits — not even for their own
        # project — matching the production ProjectCredit REST boundary. The
        # old Customer-based scoping leaked every sibling project's credit.
        fixture = ProjectFixture()
        invoice = self._ensure_customer_credit(fixture.customer)
        invoices_factories.ProjectCreditFactory(
            project=fixture.project, value=Decimal("100")
        )
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=fixture.project,
            quantity=1,
            unit_price=Decimal("300"),  # overdrawn for an authorized viewer
        )
        # Staff sees the overdrawn project...
        self.assertEqual(
            self.tool.execute(fixture.staff, {})["data"]["_total_count"], 1
        )
        # ...but a project-only member (no customer role) sees nothing.
        result = self.tool.execute(fixture.admin, {})
        self.assertEqual(result["data"]["_total_count"], 0)
        self.assertIn("No overdrawn", result["summary"])

    def test_non_staff_user_sees_only_their_customer(self):
        fixture = ProjectFixture()
        other_customer = structure_factories.CustomerFactory(name="Hidden Org")

        self._add_project_with_spend(fixture.customer, "Visible Over", "100", "300")
        self._add_project_with_spend(other_customer, "Hidden Over", "100", "300")

        result = self.tool.execute(fixture.owner, {})
        self.assertEqual(result["data"]["_total_count"], 1)
        self.assertEqual(
            result["data"]["overdrawn_projects"][0]["project_name"], "Visible Over"
        )
