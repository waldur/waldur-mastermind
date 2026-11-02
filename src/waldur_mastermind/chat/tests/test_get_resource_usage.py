import json
from datetime import date

from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APITestCase

from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tests.utils import assert_matches_output_schema
from waldur_mastermind.chat.tools.account.get_resource_usage import (
    GetResourceUsageTool,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace import models as mp_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as mp_factories


class GetResourceUsageToolTest(APITestCase):
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
        assert_matches_output_schema(self.tool, result)
        self.assertEqual(result["data"]["resource"]["name"], "test-server-1")
        components = result["data"]["components"]
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["type"], "cpu")
        self.assertEqual(components[0]["usage"], "42.00")

    def test_result_survives_the_streamer_json_encoding(self):
        # The streamer hands tool results to the model via json.dumps; a
        # Decimal in the payload kills the whole stream, not just the tool.
        result = self.tool.execute(
            self.fixture.member,
            {"resource_uuid": str(self.resource.uuid)},
        )

        json.dumps(result)

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

    def _role(self, model, name):
        return Role.objects.create(
            name=name,
            content_type=ContentType.objects.get_for_model(model),
            is_system_role=False,
        )

    def test_usage_hidden_as_in_rest(self):
        resource_project = mp_models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        rp_viewer = structure_factories.UserFactory()
        add_user(
            resource_project, rp_viewer, self._role(mp_models.ResourceProject, "RP")
        )
        resource_viewer = structure_factories.UserFactory()
        add_user(
            self.resource, resource_viewer, self._role(mp_models.Resource, "Viewer")
        )

        for label, user in (("rp-only", rp_viewer), ("resource", resource_viewer)):
            with self.subTest(user=label):
                self.client.force_authenticate(user)
                rest = self.client.get(
                    mp_factories.ResourceFactory.get_url(self.resource)
                ).data
                result = self.tool.execute(
                    user, {"resource_uuid": str(self.resource.uuid)}
                )
                assert_matches_output_schema(self.tool, result)
                data = result["data"]
                self.assertEqual(
                    data["components"] is not None, "current_usages" in rest
                )
                self.assertEqual(
                    data["restricted_fields"],
                    ["components"] if label == "rp-only" else [],
                )
                self.assertEqual("hidden" in result["summary"], label == "rp-only")
