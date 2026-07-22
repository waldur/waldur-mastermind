from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.explain_resource_paused_reason import (
    ExplainResourcePausedReasonTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


def _attribution_blob(policy_class, policy_uuid, scope_name, limit_cost):
    return {
        "policy_class": policy_class,
        "policy_uuid": policy_uuid,
        "action": "request_pausing",
        "scope_name": scope_name,
        "timestamp": "2026-04-15T08:30:00+00:00",
        "limit_cost": str(limit_cost),
        "actions": "request_pausing",
    }


class ExplainResourcePausedReasonToolTest(TestCase):
    def setUp(self):
        self.tool = ExplainResourcePausedReasonTool()
        self.fixture = ProjectFixture()

    def _make_resource(self, **kwargs):
        return marketplace_factories.ResourceFactory(
            project=self.fixture.project, **kwargs
        )

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.EXPLAIN_RESOURCE_PAUSED_REASON, tool_registry)
        self.assertEqual(
            tool_registry.get(
                ToolName.EXPLAIN_RESOURCE_PAUSED_REASON
            ).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_unknown_resource_returns_error(self):
        result = self.tool.execute(self.fixture.staff, {"resource_name": "Nonexistent"})
        self.assertEqual(result["type"], "error")

    def test_not_paused_branch(self):
        resource = self._make_resource(name="Calm VM", paused=False)
        result = self.tool.execute(
            self.fixture.staff, {"resource_uuid": str(resource.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["primary_cause"], "not_paused")
        self.assertIn("NOT paused", result["summary"])

    def test_cost_policy_attribution(self):
        # Set up the spend so explain numbers reconcile.
        invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("10000")
        )
        invoice = invoices_factories.InvoiceFactory(customer=self.fixture.customer)
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            quantity=1,
            unit_price=Decimal("250"),
        )
        attribution = _attribution_blob(
            "ProjectEstimatedCostPolicy",
            "93000000000000000000000000000001",
            self.fixture.project.name,
            limit_cost="100",
        )
        resource = self._make_resource(
            name="Overdrawn VM",
            paused=True,
            attributes={"_policy_attribution": {"paused": attribution}},
        )

        result = self.tool.execute(
            self.fixture.staff, {"resource_uuid": str(resource.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["primary_cause"], "cost_policy")
        details = result["data"]["policy_details"]
        self.assertEqual(details["policy_kind"], "ProjectEstimatedCostPolicy")
        self.assertEqual(details["limit_cost"], "100")
        self.assertEqual(Decimal(details["current_spend"]), Decimal("250"))
        self.assertEqual(Decimal(details["exceeded_by"]), Decimal("150"))
        self.assertIn("cost policy", result["summary"])

    def test_slurm_grace_attribution(self):
        attribution = _attribution_blob(
            "SlurmPeriodicPolicy",
            "93000000000000000000000000000002",
            self.fixture.project.name,
            limit_cost="0",
        )
        attribution["grace_ratio"] = "0.2"
        resource = self._make_resource(
            name="SLURM Overuse VM",
            paused=True,
            attributes={"_policy_attribution": {"paused": attribution}},
        )

        result = self.tool.execute(
            self.fixture.staff, {"resource_uuid": str(resource.uuid)}
        )
        self.assertEqual(result["data"]["primary_cause"], "slurm_grace")
        self.assertEqual(result["data"]["policy_details"]["grace_ratio"], "0.2")
        self.assertIn("SLURM", result["summary"])

    def test_manual_pause_no_attribution(self):
        resource = self._make_resource(
            name="Manual Pause VM", paused=True, attributes={}
        )
        result = self.tool.execute(
            self.fixture.staff, {"resource_uuid": str(resource.uuid)}
        )
        self.assertEqual(result["data"]["primary_cause"], "manual")
        self.assertIsNone(result["data"]["attribution"])
        self.assertIn("manual", result["summary"].lower())

    def test_project_end_date_grace_period_compounds(self):
        # Project past end_date but within grace.
        self.fixture.project.end_date = date.today() - timedelta(days=2)
        self.fixture.project.grace_period_days = 30
        self.fixture.project.save()

        attribution = _attribution_blob(
            "ProjectEstimatedCostPolicy",
            "93000000000000000000000000000003",
            self.fixture.project.name,
            limit_cost="100",
        )
        resource = self._make_resource(
            name="Compound VM",
            paused=True,
            attributes={"_policy_attribution": {"paused": attribution}},
        )

        result = self.tool.execute(
            self.fixture.staff, {"resource_uuid": str(resource.uuid)}
        )
        proj = result["data"]["project"]
        self.assertTrue(proj["is_in_grace_period"])
        self.assertIn("grace period", result["summary"])

    def test_project_member_spend_visibility_follows_billing_info_flag(self):
        # The resource and its paused attribution are project-scoped, so a
        # project member may legitimately see them. current_spend is summed
        # from InvoiceItems, whose project-scope visibility is gated by the
        # customer's display_billing_info_in_projects flag: visible by
        # default, hidden once the customer opts out.
        invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer, value=Decimal("10000")
        )
        invoice = invoices_factories.InvoiceFactory(customer=self.fixture.customer)
        invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=self.fixture.project,
            quantity=1,
            unit_price=Decimal("250"),
        )
        attribution = _attribution_blob(
            "ProjectEstimatedCostPolicy",
            "93000000000000000000000000000004",
            self.fixture.project.name,
            limit_cost="100",
        )
        resource = self._make_resource(
            name="Scoped VM",
            paused=True,
            attributes={"_policy_attribution": {"paused": attribution}},
        )
        # Staff sees the real customer spend (250).
        staff_details = self.tool.execute(
            self.fixture.staff, {"resource_uuid": str(resource.uuid)}
        )["data"]["policy_details"]
        self.assertEqual(Decimal(staff_details["current_spend"]), Decimal("250"))
        # With the default flag the project-only member sees the spend too.
        result = self.tool.execute(
            self.fixture.admin, {"resource_uuid": str(resource.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(
            Decimal(result["data"]["policy_details"]["current_spend"]), Decimal("250")
        )

        self.fixture.customer.display_billing_info_in_projects = False
        self.fixture.customer.save(update_fields=["display_billing_info_in_projects"])
        # Once the customer opts out, the member still sees the resource +
        # cause, but the billing total no longer leaks.
        result = self.tool.execute(
            self.fixture.admin, {"resource_uuid": str(resource.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["primary_cause"], "cost_policy")
        self.assertEqual(
            Decimal(result["data"]["policy_details"]["current_spend"]), Decimal("0")
        )

    def test_inaccessible_resource_returns_not_found(self):
        # A non-staff user from another project cannot see this resource.
        other = ProjectFixture()
        resource = self._make_resource(name="Hidden VM", paused=True)
        result = self.tool.execute(other.member, {"resource_uuid": str(resource.uuid)})
        self.assertEqual(result["type"], "error")

    def test_disambiguate_by_project_name(self):
        # Two resources with the same name in different projects.
        other = ProjectFixture()
        marketplace_factories.ResourceFactory(project=other.project, name="Twin VM")
        target = self._make_resource(name="Twin VM", paused=True)
        result = self.tool.execute(
            self.fixture.staff,
            {
                "resource_name": "Twin VM",
                "project_name": self.fixture.project.name,
            },
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["resource"]["uuid"], str(target.uuid))
