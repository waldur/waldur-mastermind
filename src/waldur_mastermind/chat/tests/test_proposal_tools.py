from django.test import TestCase

from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.proposals_researcher.guide_proposal import (
    GuideProposalTool,
)
from waldur_mastermind.chat.tools.proposals_researcher.proposal_overview import (
    ProposalOverviewTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.call_insights import (
    CallInsightsTool,
)
from waldur_mastermind.chat.tools.proposals_reviewer.review_assistant import (
    ReviewAssistantTool,
)
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures


class ProposalToolRegistrationTest(TestCase):
    """Verify all proposal tools are registered and have correct definitions."""

    def test_find_matching_calls_is_registered(self):
        self.assertIn(ToolName.FIND_MATCHING_CALLS, tool_registry)

    def test_guide_proposal_is_registered(self):
        self.assertIn(ToolName.GUIDE_PROPOSAL, tool_registry)

    def test_review_workload_is_registered(self):
        self.assertIn(ToolName.REVIEW_WORKLOAD, tool_registry)

    def test_call_insights_is_registered(self):
        self.assertIn(ToolName.CALL_INSIGHTS, tool_registry)

    def test_proposal_overview_is_registered(self):
        self.assertIn(ToolName.PROPOSAL_OVERVIEW, tool_registry)

    def test_review_assistant_is_registered(self):
        self.assertIn(ToolName.REVIEW_ASSISTANT, tool_registry)

    def test_list_calls_is_registered(self):
        self.assertIn(ToolName.LIST_CALLS, tool_registry.definitions)

    def test_list_proposals_is_registered(self):
        self.assertIn(ToolName.LIST_PROPOSALS, tool_registry.definitions)


class ProposalToolDefinitionTest(TestCase):
    """Verify tool definitions have correct structure."""

    def test_find_matching_calls_definition(self):
        tool = tool_registry.get(ToolName.FIND_MATCHING_CALLS)
        self.assertEqual(tool.definition.name, ToolName.FIND_MATCHING_CALLS)
        self.assertIn("keywords", tool.definition.inputSchema["properties"])
        self.assertIn("keywords", tool.definition.inputSchema["required"])
        self.assertEqual(
            tool.definition.inputSchema["properties"]["keywords"]["type"], "array"
        )
        self.assertNotEqual(tool.definition.usage_instructions, "")

    def test_guide_proposal_definition(self):
        tool = tool_registry.get(ToolName.GUIDE_PROPOSAL)
        self.assertEqual(tool.definition.name, ToolName.GUIDE_PROPOSAL)
        properties = tool.definition.inputSchema["properties"]
        self.assertIn("uuid", properties)
        self.assertIn("name", properties)
        # At-least-one is enforced server-side, not via JSON Schema "required".
        self.assertEqual(
            tool.definition.inputSchema.get("required", []),
            [],
        )

    def test_review_workload_definition(self):
        tool = tool_registry.get(ToolName.REVIEW_WORKLOAD)
        self.assertEqual(tool.definition.name, ToolName.REVIEW_WORKLOAD)
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_call_insights_definition(self):
        tool = tool_registry.get(ToolName.CALL_INSIGHTS)
        self.assertEqual(tool.definition.name, ToolName.CALL_INSIGHTS)
        properties = tool.definition.inputSchema["properties"]
        self.assertIn("uuid", properties)
        self.assertIn("name", properties)
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_proposal_overview_definition(self):
        tool = tool_registry.get(ToolName.PROPOSAL_OVERVIEW)
        self.assertEqual(tool.definition.name, ToolName.PROPOSAL_OVERVIEW)
        properties = tool.definition.inputSchema["properties"]
        self.assertIn("uuid", properties)
        self.assertIn("slug", properties)
        self.assertIn("name", properties)
        self.assertEqual(
            tool.definition.inputSchema.get("required", []),
            [],
        )

    def test_review_assistant_definition(self):
        tool = tool_registry.get(ToolName.REVIEW_ASSISTANT)
        self.assertEqual(tool.definition.name, ToolName.REVIEW_ASSISTANT)
        properties = tool.definition.inputSchema["properties"]
        self.assertIn("uuid", properties)
        self.assertIn("slug", properties)
        self.assertIn("name", properties)
        self.assertEqual(
            tool.definition.inputSchema.get("required", []),
            [],
        )

    def test_list_calls_definition(self):
        d = tool_registry.get(ToolName.LIST_CALLS).definition
        self.assertEqual(d.category, ToolCategory.PROPOSALS_RESEARCHER)
        # Workflow guidance lives only on list_calls (entry point of funnel).
        self.assertIn("PROPOSALS DISCOVERY FUNNEL", d.workflow_instructions)

    def test_list_proposals_definition(self):
        d = tool_registry.get(ToolName.LIST_PROPOSALS).definition
        self.assertEqual(d.category, ToolCategory.PROPOSALS_RESEARCHER)
        props = d.inputSchema["properties"]
        for field in ("call_uuid", "call_name", "state", "mine", "search"):
            self.assertIn(field, props)


class ProposalToolSetsTest(TestCase):
    """Verify proposal tools appear in correct tool sets."""

    def test_find_matching_calls_in_all_sets(self):
        self.assertIn(ToolName.FIND_MATCHING_CALLS, STAFF_TOOLS)
        self.assertIn(ToolName.FIND_MATCHING_CALLS, SUPPORT_TOOLS)
        self.assertIn(ToolName.FIND_MATCHING_CALLS, END_USER_TOOLS)

    def test_guide_proposal_in_all_sets(self):
        self.assertIn(ToolName.GUIDE_PROPOSAL, STAFF_TOOLS)
        self.assertIn(ToolName.GUIDE_PROPOSAL, SUPPORT_TOOLS)
        self.assertIn(ToolName.GUIDE_PROPOSAL, END_USER_TOOLS)

    def test_review_workload_in_all_sets(self):
        self.assertIn(ToolName.REVIEW_WORKLOAD, STAFF_TOOLS)
        self.assertIn(ToolName.REVIEW_WORKLOAD, SUPPORT_TOOLS)
        self.assertIn(ToolName.REVIEW_WORKLOAD, END_USER_TOOLS)

    def test_call_insights_only_in_staff(self):
        self.assertIn(ToolName.CALL_INSIGHTS, STAFF_TOOLS)
        self.assertNotIn(ToolName.CALL_INSIGHTS, SUPPORT_TOOLS)
        self.assertNotIn(ToolName.CALL_INSIGHTS, END_USER_TOOLS)

    def test_proposal_overview_in_all_sets(self):
        self.assertIn(ToolName.PROPOSAL_OVERVIEW, STAFF_TOOLS)
        self.assertIn(ToolName.PROPOSAL_OVERVIEW, SUPPORT_TOOLS)
        self.assertIn(ToolName.PROPOSAL_OVERVIEW, END_USER_TOOLS)

    def test_review_assistant_in_all_sets(self):
        self.assertIn(ToolName.REVIEW_ASSISTANT, STAFF_TOOLS)
        self.assertIn(ToolName.REVIEW_ASSISTANT, SUPPORT_TOOLS)
        self.assertIn(ToolName.REVIEW_ASSISTANT, END_USER_TOOLS)

    def test_list_calls_in_all_user_sets(self):
        for tool_set in (STAFF_TOOLS, SUPPORT_TOOLS, END_USER_TOOLS):
            self.assertIn(ToolName.LIST_CALLS, tool_set)

    def test_list_proposals_in_all_user_sets(self):
        for tool_set in (STAFF_TOOLS, SUPPORT_TOOLS, END_USER_TOOLS):
            self.assertIn(ToolName.LIST_PROPOSALS, tool_set)


class ProposalToolOpenAIFormatTest(TestCase):
    """Verify tools produce valid OpenAI function-calling format."""

    def test_all_proposal_tools_in_openai_format(self):
        proposal_tools = [
            ToolName.FIND_MATCHING_CALLS,
            ToolName.GUIDE_PROPOSAL,
            ToolName.REVIEW_WORKLOAD,
            ToolName.CALL_INSIGHTS,
            ToolName.PROPOSAL_OVERVIEW,
            ToolName.REVIEW_ASSISTANT,
            ToolName.LIST_CALLS,
            ToolName.LIST_PROPOSALS,
        ]
        openai_tools = tool_registry.get_openai_tools(proposal_tools)
        self.assertEqual(len(openai_tools), 8)

        for tool_spec in openai_tools:
            self.assertEqual(tool_spec["type"], "function")
            self.assertIn("name", tool_spec["function"])
            self.assertIn("description", tool_spec["function"])
            self.assertIn("parameters", tool_spec["function"])
            self.assertEqual(tool_spec["function"]["parameters"]["type"], "object")


class GuideProposalToolExecuteTest(TestCase):
    def setUp(self):
        self.tool = GuideProposalTool()
        self.fixture = proposal_fixtures.ProposalFixture()
        # Ensure the call exists.
        self.call = self.fixture.call

    def test_requires_uuid_or_name(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_resolves_by_uuid(self):
        result = self.tool.execute(self.fixture.staff, {"uuid": str(self.call.uuid)})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["name"], self.call.name)

    def test_resolves_by_name(self):
        result = self.tool.execute(self.fixture.staff, {"name": self.call.name})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["name"], self.call.name)

    def test_unknown_call_returns_error(self):
        result = self.tool.execute(
            self.fixture.staff, {"name": "definitely-not-a-call"}
        )
        self.assertEqual(result["type"], "error")


class ProposalOverviewToolExecuteTest(TestCase):
    def setUp(self):
        self.tool = ProposalOverviewTool()
        self.fixture = proposal_fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal_submitted

    def test_requires_at_least_one_arg(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_resolves_by_uuid(self):
        result = self.tool.execute(
            self.fixture.staff, {"uuid": str(self.proposal.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["slug"], self.proposal.slug)

    def test_resolves_by_slug(self):
        result = self.tool.execute(self.fixture.staff, {"slug": self.proposal.slug})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["slug"], self.proposal.slug)

    def test_resolves_by_name(self):
        result = self.tool.execute(self.fixture.staff, {"name": self.proposal.name})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["slug"], self.proposal.slug)

    def test_unknown_proposal_returns_error(self):
        result = self.tool.execute(self.fixture.staff, {"slug": "no-such-slug"})
        self.assertEqual(result["type"], "error")


class CallInsightsToolExecuteTest(TestCase):
    def setUp(self):
        self.tool = CallInsightsTool()
        self.fixture = proposal_fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_no_args_returns_full_list(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "success")
        self.assertIn("insights", result["data"])

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_resolves_by_uuid(self):
        result = self.tool.execute(self.fixture.staff, {"uuid": str(self.call.uuid)})
        self.assertEqual(result["type"], "success")
        insights = result["data"]["insights"]
        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0]["call_uuid"], str(self.call.uuid))

    def test_resolves_by_name(self):
        result = self.tool.execute(self.fixture.staff, {"name": self.call.name})
        self.assertEqual(result["type"], "success")
        insights = result["data"]["insights"]
        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0]["call_name"], self.call.name)

    def test_unknown_call_returns_error(self):
        result = self.tool.execute(
            self.fixture.staff, {"name": "definitely-not-a-call"}
        )
        self.assertEqual(result["type"], "error")


class ReviewAssistantToolExecuteTest(TestCase):
    def setUp(self):
        self.tool = ReviewAssistantTool()
        self.fixture = proposal_fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal_submitted
        # Touch reviewer_1 + review so the staff path passes the assignment check.
        self.review = self.fixture.review

    def test_requires_at_least_one_arg(self):
        result = self.tool.execute(self.fixture.staff, {})
        self.assertEqual(result["type"], "validation_error")

    def test_invalid_uuid_returns_validation_error(self):
        result = self.tool.execute(self.fixture.staff, {"uuid": "not-a-uuid"})
        self.assertEqual(result["type"], "validation_error")

    def test_resolves_by_uuid(self):
        result = self.tool.execute(
            self.fixture.staff, {"uuid": str(self.proposal.uuid)}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["proposal"]["slug"], self.proposal.slug)

    def test_resolves_by_slug(self):
        result = self.tool.execute(self.fixture.staff, {"slug": self.proposal.slug})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["proposal"]["slug"], self.proposal.slug)

    def test_resolves_by_name(self):
        # Give the proposal a distinctive name so icontains finds it cleanly.
        self.proposal.name = "Distinctive Proposal Title"
        self.proposal.save()
        result = self.tool.execute(self.fixture.staff, {"name": "Distinctive"})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["proposal"]["slug"], self.proposal.slug)

    def test_unknown_proposal_returns_error(self):
        result = self.tool.execute(self.fixture.staff, {"slug": "no-such-slug"})
        self.assertEqual(result["type"], "error")
