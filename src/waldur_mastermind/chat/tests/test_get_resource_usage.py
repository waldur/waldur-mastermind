from datetime import date

from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_resource_usage import (
    GetResourceUsageTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace import models as mp_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as mp_factories


class GetResourceUsageToolTest(TestCase):
    def setUp(self):
        self.tool = GetResourceUsageTool()
        self.fixture = ProjectFixture()
        self.offering = mp_factories.OfferingFactory(
            customer=self.fixture.customer, name="Test Offering"
        )
        self.resource = mp_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            name="test-server-1",
            state=ResourceStates.OK,
        )
        self.component = mp_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="cpu",
            name="CPU",
            measured_unit="hours",
        )
        today = date.today()
        period_start = today.replace(day=1)
        mp_models.ComponentUsage.objects.create(
            resource=self.resource,
            component=self.component,
            usage=42,
            date=today,
            billing_period=period_start,
        )

    def test_registered_with_account_category(self):
        self.assertIn(ToolName.GET_RESOURCE_USAGE, tool_registry)
        self.assertEqual(
            tool_registry.get(ToolName.GET_RESOURCE_USAGE).definition.category,
            ToolCategory.ACCOUNT,
        )

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.member, {})
        self.assertEqual(result["type"], "validation_error")

    def test_returns_components_and_period(self):
        result = self.tool.execute(
            self.fixture.member,
            {"resource_uuid": str(self.resource.uuid)},
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["resource"]["name"], "test-server-1")
        components = result["data"]["components"]
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["type"], "cpu")
        self.assertEqual(components[0]["usage"], 42)

    def test_name_fallback(self):
        result = self.tool.execute(
            self.fixture.member,
            {"resource_name": "test-server"},
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["resource"]["name"], "test-server-1")

    def test_not_accessible_resource_returns_not_found(self):
        other_project = structure_factories.ProjectFactory()
        other_resource = mp_factories.ResourceFactory(
            project=other_project,
            offering=mp_factories.OfferingFactory(),
            state=ResourceStates.OK,
        )
        result = self.tool.execute(
            self.fixture.member,
            {"resource_uuid": str(other_resource.uuid)},
        )
        self.assertEqual(result["type"], "error")
