from django.test import TestCase

from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
)


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


class ProposalToolDefinitionTest(TestCase):
    """Verify tool definitions have correct structure."""

    def test_find_matching_calls_definition(self):
        tool = tool_registry.get(ToolName.FIND_MATCHING_CALLS)
        self.assertEqual(tool.definition.name, ToolName.FIND_MATCHING_CALLS)
        self.assertIn("research_description", tool.definition.inputSchema["properties"])
        self.assertIn("research_description", tool.definition.inputSchema["required"])
        self.assertNotEqual(tool.definition.usage_instructions, "")

    def test_guide_proposal_definition(self):
        tool = tool_registry.get(ToolName.GUIDE_PROPOSAL)
        self.assertEqual(tool.definition.name, ToolName.GUIDE_PROPOSAL)
        self.assertIn("call_name_or_uuid", tool.definition.inputSchema["properties"])
        self.assertIn("call_name_or_uuid", tool.definition.inputSchema["required"])

    def test_review_workload_definition(self):
        tool = tool_registry.get(ToolName.REVIEW_WORKLOAD)
        self.assertEqual(tool.definition.name, ToolName.REVIEW_WORKLOAD)
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_call_insights_definition(self):
        tool = tool_registry.get(ToolName.CALL_INSIGHTS)
        self.assertEqual(tool.definition.name, ToolName.CALL_INSIGHTS)
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_proposal_overview_definition(self):
        tool = tool_registry.get(ToolName.PROPOSAL_OVERVIEW)
        self.assertEqual(tool.definition.name, ToolName.PROPOSAL_OVERVIEW)
        self.assertIn("proposal_identifier", tool.definition.inputSchema["properties"])
        self.assertIn("proposal_identifier", tool.definition.inputSchema["required"])

    def test_review_assistant_definition(self):
        tool = tool_registry.get(ToolName.REVIEW_ASSISTANT)
        self.assertEqual(tool.definition.name, ToolName.REVIEW_ASSISTANT)
        self.assertIn("proposal_identifier", tool.definition.inputSchema["properties"])
        self.assertIn("proposal_identifier", tool.definition.inputSchema["required"])


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
        ]
        openai_tools = tool_registry.get_openai_tools(proposal_tools)
        self.assertEqual(len(openai_tools), 6)

        for tool_spec in openai_tools:
            self.assertEqual(tool_spec["type"], "function")
            self.assertIn("name", tool_spec["function"])
            self.assertIn("description", tool_spec["function"])
            self.assertIn("parameters", tool_spec["function"])
            self.assertEqual(tool_spec["function"]["parameters"]["type"], "object")
