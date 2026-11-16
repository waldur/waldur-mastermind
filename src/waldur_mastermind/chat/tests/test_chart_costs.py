import json
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from waldur_core.core.models import Feature
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat.block_schemas import BlockSerializer
from waldur_mastermind.chat.parsers import StreamParser
from waldur_mastermind.chat.tests.test_hpc_tools import persisted_block
from waldur_mastermind.chat.tests.utils import assert_matches_output_schema
from waldur_mastermind.chat.tools.account.chart_costs import ChartCostsTool
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
    get_tool_set_for_user,
)
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoices_factories


def chart_block(result: dict) -> dict:
    """The block the thread stores for a chart result, through the same
    parser the stream uses, validated as a stored block."""
    ui = StreamParser().parse_tool_result(result)
    assert ui["k"] == "chart", ui
    block = persisted_block(json.loads(json.dumps(ui)))
    serializer = BlockSerializer(data=block)
    assert serializer.is_valid(), serializer.errors
    return block


class ChartCostsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.tool = ChartCostsTool()
        self.fixture = ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.this_month = timezone.localdate().replace(day=1)
        self.last_month = self.this_month - relativedelta(months=1)

    def _item(self, month, unit_price, project=None, **kwargs):
        invoice = invoice_models.Invoice.objects.filter(
            customer=self.customer, year=month.year, month=month.month
        ).first() or invoices_factories.InvoiceFactory(
            customer=self.customer, year=month.year, month=month.month
        )
        return invoices_factories.InvoiceItemFactory(
            invoice=invoice,
            project=project or self.project,
            quantity=1,
            unit_price=Decimal(unit_price),
            **kwargs,
        )

    def _month(self, result, month):
        return next(
            row
            for row in result["data"]["months"]
            if (row["year"], row["month"]) == (month.year, month.month)
        )

    def test_registered_for_every_role(self):
        definition = tool_registry.get(ToolName.CHART_COSTS).definition
        self.assertEqual(definition.category, ToolCategory.ACCOUNT)
        self.assertTrue(definition.read_only)
        for tool_set in (END_USER_TOOLS, SUPPORT_TOOLS, STAFF_TOOLS):
            self.assertIn(ToolName.CHART_COSTS, tool_set)

    def test_owner_gets_organization_chart(self):
        self._item(self.last_month, "100")
        self._item(self.this_month, "40")

        result = self.tool.execute(
            self.fixture.owner, {"customer_uuid": self.customer.uuid.hex}
        )

        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["data"]["months"]), 12)
        self.assertEqual(self._month(result, self.last_month)["price"], 100.0)
        self.assertEqual(self._month(result, self.this_month)["price"], 40.0)
        self.assertEqual(result["data"]["total_price"], 140.0)
        assert_matches_output_schema(self.tool, result)
        block = chart_block(result)
        self.assertEqual(block["chart_type"], "stacked_bar")
        self.assertTrue(block["x"]["categories"][-1].endswith("(estimate)"))
        self.assertEqual(
            block["link"]["url"],
            f"/organizations/{self.customer.uuid.hex}/dashboard/",
        )

    def test_compensation_is_not_subtracted_twice(self):
        self._item(self.last_month, "100")
        self._item(self.last_month, "-30")

        result = self.tool.execute(
            self.fixture.owner,
            {"customer_name": self.customer.name, "months": 3},
        )

        row = self._month(result, self.last_month)
        self.assertEqual(
            (row["incurred"], row["compensation"], row["price"]),
            (100.0, -30.0, 70.0),
        )
        self.assertEqual(result["data"]["total_price"], 70.0)
        block = chart_block(result)
        self.assertEqual(
            [s["name"] for s in block["series"]],
            ["Cost", "Credit and compensation"],
        )

    def test_same_figures_as_costs_endpoint(self):
        self._item(self.last_month, "100")
        self._item(self.last_month, "-30")
        self.client.force_login(self.fixture.owner)

        response = self.client.get(
            invoices_factories.InvoiceItemFactory.get_list_url("costs"),
            {"project_uuid": self.project.uuid.hex},
        )
        result = self.tool.execute(
            self.fixture.owner, {"project_uuid": self.project.uuid.hex}
        )

        endpoint = next(
            row
            for row in response.json()
            if (row["year"], row["month"])
            == (self.last_month.year, self.last_month.month)
        )
        row = self._month(result, self.last_month)
        for key in ("price", "compensation", "incurred"):
            self.assertEqual(Decimal(endpoint[key]), Decimal(str(row[key])), key)

    def test_project_member_sees_project_when_organization_shares_billing(self):
        self.customer.display_billing_info_in_projects = True
        self.customer.save()
        self._item(self.this_month, "25")

        result = self.tool.execute(
            self.fixture.member, {"project_name": self.project.name}
        )

        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["total_price"], 25.0)
        self.assertEqual(
            chart_block(result)["link"]["url"], f"/projects/{self.project.uuid.hex}/"
        )

    def test_project_member_refused_when_organization_hides_billing(self):
        self.customer.display_billing_info_in_projects = False
        self.customer.save()

        result = self.tool.execute(
            self.fixture.member, {"project_uuid": self.project.uuid.hex}
        )

        self.assertEqual(result["type"], "validation_error")
        self.assertNotIn("ui_component", result)

    def test_project_member_refused_for_organization(self):
        self.customer.display_billing_info_in_projects = True
        self.customer.save()

        result = self.tool.execute(
            self.fixture.member, {"customer_uuid": self.customer.uuid.hex}
        )

        self.assertEqual(result["type"], "validation_error")

    def test_outsider_cannot_find_organization(self):
        outsider = structure_factories.UserFactory()

        result = self.tool.execute(outsider, {"customer_uuid": self.customer.uuid.hex})

        self.assertEqual(result["type"], "validation_error")
        self.assertNotIn("data", result)

    def test_staff_and_support_see_any_organization(self):
        self._item(self.this_month, "10")
        for user in (
            self.fixture.staff,
            structure_factories.UserFactory(is_support=True),
        ):
            result = self.tool.execute(user, {"customer_uuid": self.customer.uuid.hex})
            self.assertEqual(result["data"]["total_price"], 10.0)

    def test_fixed_price_organization_gets_no_chart(self):
        invoices_factories.PaymentProfileFactory(
            organization=self.customer,
            payment_type=invoice_models.PaymentType.FIXED_PRICE,
            is_active=True,
        )

        result = self.tool.execute(
            self.fixture.owner, {"customer_uuid": self.customer.uuid.hex}
        )

        self.assertEqual(result["type"], "success")
        self.assertTrue(result["data"]["fixed_price"])
        assert_matches_output_schema(self.tool, result)
        self.assertNotIn("ui_component", result)

    def test_needs_a_scope(self):
        result = self.tool.execute(self.fixture.owner, {})
        self.assertEqual(result["type"], "validation_error")

    def test_organization_narrows_the_project(self):
        other = structure_factories.ProjectFactory(name=self.project.name)
        other.customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        self._item(self.this_month, "15")

        ambiguous = self.tool.execute(
            self.fixture.owner, {"project_name": self.project.name}
        )
        self.assertEqual(ambiguous["type"], "validation_error")
        self.assertIn(self.customer.name, ambiguous["summary"])

        result = self.tool.execute(
            self.fixture.owner,
            {
                "project_name": self.project.name,
                "customer_uuid": self.customer.uuid.hex,
            },
        )
        self.assertEqual(result["data"]["project_uuid"], self.project.uuid.hex)
        self.assertEqual(result["data"]["total_price"], 15.0)

    def test_months_sets_the_window(self):
        self._item(self.this_month - relativedelta(months=3), "70")
        self._item(self.last_month, "20")

        one = self.tool.execute(
            self.fixture.owner, {"customer_uuid": self.customer.uuid.hex, "months": 0}
        )
        two = self.tool.execute(
            self.fixture.owner, {"customer_uuid": self.customer.uuid.hex, "months": 2}
        )

        self.assertEqual(len(one["data"]["months"]), 1)
        self.assertEqual(len(two["data"]["months"]), 2)
        # The item three months back is outside both windows.
        self.assertEqual(two["data"]["total_price"], 20.0)

    def test_payment_counts_in_the_month_it_pays(self):
        self._item(self.last_month, "100")
        # Invoiced this month, but paying last month's cost.
        self._item(self.this_month, "-40", compensated_period=self.last_month)

        result = self.tool.execute(
            self.fixture.owner, {"customer_uuid": self.customer.uuid.hex, "months": 2}
        )

        self.assertEqual(self._month(result, self.last_month)["price"], 60.0)
        self.assertEqual(self._month(result, self.this_month)["price"], 0.0)

    def test_no_costs_shows_the_empty_message(self):
        result = self.tool.execute(
            self.fixture.owner, {"customer_uuid": self.customer.uuid.hex}
        )

        block = chart_block(result)
        self.assertEqual([s["points"] for s in block["series"]], [[]])
        self.assertEqual(len(result["data"]["months"]), 12)

    def test_long_project_name_keeps_the_chart(self):
        self.project.name = "p" * 400
        self.project.save()
        self._item(self.this_month, "5")

        result = self.tool.execute(
            self.fixture.owner, {"project_uuid": self.project.uuid.hex}
        )

        self.assertLessEqual(len(chart_block(result)["title"]), 200)

    def test_ambiguous_name_asks_for_uuid(self):
        structure_factories.ProjectFactory(
            customer=self.customer, name=f"{self.project.name} two"
        )
        structure_factories.ProjectFactory(
            customer=self.customer, name=f"{self.project.name} three"
        )

        result = self.tool.execute(self.fixture.owner, {"project_name": "two"})
        self.assertEqual(result["type"], "success")

        self.project.name = "shared"
        self.project.save()
        structure_factories.ProjectFactory(customer=self.customer, name="shared-2")
        result = self.tool.execute(self.fixture.owner, {"project_name": "shar"})
        self.assertEqual(result["type"], "validation_error")
        self.assertIn("Pass a UUID", result["summary"])


class ConcealedPricesTest(TestCase):
    def setUp(self):
        cache.clear()
        self.fixture = ProjectFixture()

    def test_not_offered_while_prices_are_concealed(self):
        for user in (self.fixture.owner, self.fixture.staff):
            self.assertIn(ToolName.CHART_COSTS, get_tool_set_for_user(user))

        Feature.objects.create(key="marketplace.conceal_prices", value=True)
        deployment_profile.invalidate_cache()
        # The profile is cached across tests; do not leak concealed prices.
        self.addCleanup(deployment_profile.invalidate_cache)

        for user in (self.fixture.owner, self.fixture.staff):
            self.assertNotIn(ToolName.CHART_COSTS, get_tool_set_for_user(user))


class ChartFittingTest(TestCase):
    def test_text_from_user_data_is_cut_not_dropped(self):
        # As plot_hpc_output builds from a CSV with a blank and a long header.
        result = {
            "type": "success",
            "summary": "Plotted.",
            "ui_component": "chart",
            "ui_data": {
                "chart_type": "line",
                "title": "t" * 250,
                "x": {"type": "value"},
                "y": {"type": "value"},
                "series": [
                    {"name": "", "points": [[0, 1]]},
                    {"name": "n" * 150, "points": [[0, 2]], "labels": ["l" * 400]},
                ],
            },
        }

        block = chart_block(result)

        self.assertEqual(block["series"][0]["name"], "Series 1")
        self.assertEqual(len(block["series"][1]["name"]), 100)
        self.assertEqual(len(block["series"][1]["labels"][0]), 300)
        self.assertEqual(len(block["title"]), 200)

    def test_untitled_plot_keeps_its_chart(self):
        # A GROMACS .xvg file may carry `@ title ""`.
        result = {
            "type": "success",
            "summary": "Plotted.",
            "ui_component": "chart",
            "ui_data": {
                "chart_type": "line",
                "title": "",
                "x": {"type": "value"},
                "y": {"type": "value"},
                "series": [{"name": "a", "points": [[0, 1]]}],
            },
        }

        self.assertEqual(chart_block(result)["title"], "Chart")


class InvalidChartTest(TestCase):
    def test_invalid_chart_falls_back_to_the_summary(self):
        result = {
            "type": "success",
            "summary": "Costs went up.",
            "ui_component": "chart",
            # Required fields present, but a point with one value is invalid.
            "ui_data": {
                "chart_type": "bar",
                "title": "Costs",
                "x": {"type": "category"},
                "y": {"type": "value"},
                "series": [{"name": "Cost", "points": [[1]]}],
            },
        }

        ui = StreamParser().parse_tool_result(result)

        self.assertEqual(ui["k"], "markdown")
        self.assertEqual(ui["c"], "Costs went up.")
