from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_resource import (
    RECENT_EVENTS_LIMIT,
    GetResourceTool,
)
from waldur_mastermind.chat.tools.base import MAX_LIST_RESULTS
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.models import Resource, ResourceProject
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

ROLES = ("owner", "member", "user", "global_support", "staff")


class GetResourceToolTest(APITestCase):
    def setUp(self):
        self.tool = GetResourceTool()
        self.fixture = ProjectFixture()
        self.plan = marketplace_factories.PlanFactory(name="Small")
        self.resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.plan.offering,
            plan=self.plan,
            name="web-1",
            state=ResourceStates.ERRED,
            error_message="Quota exceeded",
        )

    def _rest_retrieve_status(self, user, resource):
        self.client.force_authenticate(user)
        return self.client.get(
            marketplace_factories.ResourceFactory.get_url(resource)
        ).status_code

    def _rest_event_messages(self, user, resource):
        self.client.force_authenticate(user)
        response = self.client.get(
            logging_factories.EventFactory.get_list_url(),
            {
                "scope": "http://testserver"
                + marketplace_factories.ResourceFactory.get_url(resource)
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [e["message"] for e in response.data][:RECENT_EVENTS_LIMIT]

    def test_registered_in_account_category_and_role_sets(self):
        self.assertEqual(
            tool_registry.get(ToolName.GET_RESOURCE).definition.category,
            ToolCategory.ACCOUNT,
        )
        for tool_set in (END_USER_TOOLS, SUPPORT_TOOLS, STAFF_TOOLS):
            self.assertIn(ToolName.GET_RESOURCE, tool_set)

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_uuid_is_rejected(self):
        result = self.tool.execute(self.fixture.staff, {"resource_uuid": "nope"})
        self.assertEqual(result["type"], "validation_error")

    def test_visibility_matches_rest_retrieve(self):
        for role in ROLES:
            with self.subTest(role=role):
                user = getattr(self.fixture, role)
                rest_visible = (
                    self._rest_retrieve_status(user, self.resource)
                    == status.HTTP_200_OK
                )
                result = self.tool.execute(
                    user, {"resource_uuid": str(self.resource.uuid)}
                )
                self.assertEqual(result["type"] == "success", rest_visible)

    def test_unrelated_user_cannot_see_resource(self):
        result = self.tool.execute(
            self.fixture.user, {"resource_uuid": str(self.resource.uuid)}
        )
        self.assertEqual(result["type"], "error")

    def test_project_role_without_list_resources_cannot_see_resource(self):
        # The fixture grants LIST_RESOURCES when the admin is first created.
        admin = self.fixture.admin
        ProjectRole.ADMIN.delete_permission(PermissionEnum.LIST_RESOURCES)
        result = self.tool.execute(admin, {"resource_uuid": str(self.resource.uuid)})
        self.assertEqual(result["type"], "error")

    def test_anonymous_cannot_see_resource(self):
        result = self.tool.execute(
            AnonymousUser(), {"resource_uuid": str(self.resource.uuid)}
        )
        self.assertEqual(result["type"], "error")

    def _role(self, model, name):
        return Role.objects.create(
            name=name,
            content_type=ContentType.objects.get_for_model(model),
            is_system_role=False,
        )

    def test_limits_and_error_hidden_as_in_rest(self):
        self.resource.limits = {"cpu": 4}
        self.resource.save()
        resource_project = ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        rp_viewer = UserFactory()
        add_user(resource_project, rp_viewer, self._role(ResourceProject, "RP"))
        resource_viewer = UserFactory()
        add_user(self.resource, resource_viewer, self._role(Resource, "Viewer"))

        for label, user in (("rp-only", rp_viewer), ("resource", resource_viewer)):
            with self.subTest(user=label):
                self.client.force_authenticate(user)
                rest = self.client.get(
                    marketplace_factories.ResourceFactory.get_url(self.resource)
                ).data
                result = self.tool.execute(
                    user, {"resource_uuid": str(self.resource.uuid)}
                )
                data = result["data"]["resource"]
                self.assertEqual(
                    data["limits"] is not None, "limits" in rest, data["limits"]
                )
                self.assertEqual(bool(data["error_message"]), "error_message" in rest)
                self.assertEqual(
                    "Quota exceeded" in result["summary"], "error_message" in rest
                )
                self.assertEqual(data["limits"] is None, label == "rp-only")
                self.assertEqual(data["error_message"] is None, label == "rp-only")
                self.assertEqual(
                    data["restricted_fields"],
                    ["error_message", "limits", "recent_events"]
                    if label == "rp-only"
                    else [],
                )
                self.assertEqual(
                    result["data"]["recent_events"] is None, label == "rp-only"
                )
                self.assertEqual(
                    "hidden from this user" in result["summary"].lower(),
                    label == "rp-only",
                )

    def test_returns_details_offering_plan_and_error(self):
        result = self.tool.execute(
            self.fixture.member, {"resource_uuid": str(self.resource.uuid)}
        )
        self.assertEqual(result["type"], "success")
        data = result["data"]
        self.assertEqual(data["resource"]["uuid"], str(self.resource.uuid))
        self.assertEqual(data["resource"]["state"], "Erred")
        self.assertEqual(data["resource"]["error_message"], "Quota exceeded")
        self.assertEqual(data["offering"]["uuid"], str(self.plan.offering.uuid))
        self.assertEqual(data["plan"], {"uuid": str(self.plan.uuid), "name": "Small"})
        self.assertEqual(data["project"]["uuid"], str(self.fixture.project.uuid))
        self.assertEqual(data["organization"]["uuid"], str(self.fixture.customer.uuid))
        self.assertEqual(data["matches"], [])
        self.assertIn("Quota exceeded", result["summary"])

    def test_recent_events_match_rest_event_list(self):
        for _ in range(RECENT_EVENTS_LIMIT + 2):
            logging_factories.FeedFactory(scope=self.resource)
        logging_factories.FeedFactory(scope=self.fixture.project)

        for role in ROLES:
            with self.subTest(role=role):
                user = getattr(self.fixture, role)
                result = self.tool.execute(
                    user, {"resource_uuid": str(self.resource.uuid)}
                )
                if result["type"] != "success":
                    continue
                messages = [e["message"] for e in result["data"]["recent_events"]]
                self.assertEqual(len(messages), RECENT_EVENTS_LIMIT)
                self.assertEqual(
                    messages, self._rest_event_messages(user, self.resource)
                )

    def test_lookup_by_name_within_project(self):
        result = self.tool.execute(
            self.fixture.member,
            {"resource_name": "WEB", "project_name": self.fixture.project.name},
        )
        self.assertEqual(result["data"]["resource"]["uuid"], str(self.resource.uuid))

    def test_name_lookup_skips_terminated_but_uuid_finds_it(self):
        old = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            name="web-1",
            state=ResourceStates.TERMINATED,
        )
        result = self.tool.execute(self.fixture.member, {"resource_name": "web-1"})
        self.assertEqual(result["data"]["resource"]["uuid"], str(self.resource.uuid))
        result = self.tool.execute(
            self.fixture.member, {"resource_uuid": str(old.uuid)}
        )
        self.assertEqual(result["data"]["resource"]["state"], "Terminated")

    def test_exact_name_wins_over_partial_matches(self):
        marketplace_factories.ResourceFactory(
            project=self.fixture.project, name="web-1-old"
        )
        result = self.tool.execute(self.fixture.member, {"resource_name": "WEB-1"})
        self.assertEqual(result["data"]["resource"]["uuid"], str(self.resource.uuid))
        result = self.tool.execute(self.fixture.member, {"resource_name": "web-"})
        self.assertIsNone(result["data"]["resource"])
        self.assertEqual(len(result["data"]["matches"]), 2)

    def test_several_name_matches_are_listed_and_capped(self):
        for i in range(MAX_LIST_RESULTS + 1):
            marketplace_factories.ResourceFactory(
                project=self.fixture.project, name=f"web-{i + 2}"
            )
        result = self.tool.execute(self.fixture.member, {"resource_name": "web"})
        data = result["data"]
        self.assertIsNone(data["resource"])
        self.assertEqual(len(data["matches"]), MAX_LIST_RESULTS)
        self.assertEqual(data["_total_count"], MAX_LIST_RESULTS + 2)
        self.assertTrue(data["_truncated"])
        self.assertIn("narrow by project_name", result["summary"])

    def test_ambiguous_summary_skips_a_hint_already_followed(self):
        marketplace_factories.ResourceFactory(
            project=self.fixture.project, name="web-2"
        )
        result = self.tool.execute(
            self.fixture.member,
            {"resource_name": "web", "project_name": self.fixture.project.name},
        )
        self.assertEqual(result["summary"], "2 resources match 'web'. Ask which one.")
