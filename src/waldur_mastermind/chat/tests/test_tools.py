from unittest.mock import MagicMock, patch

from django.test import TestCase
from pydantic import ValidationError

from waldur_mastermind.chat.prompts.assembly import SYSTEM_PROMPT_TEMPLATE
from waldur_mastermind.chat.prompts.persona import PERSONA_TEMPLATE
from waldur_mastermind.chat.tools import tool_sets
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import ToolRegistry, tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    _MARKETPLACE_TOOLS,
    ANONYMOUS_TOOLS,
    END_USER_TOOLS,
    STAFF_TOOLS,
    SUPPORT_TOOLS,
    get_tool_set_for_user,
)


class ToolDefinitionTest(TestCase):
    def test_tool_definition_fields(self):
        definition = ToolDefinition(
            name=ToolName.DISPLAY_USER_RESOURCES,
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.name, ToolName.DISPLAY_USER_RESOURCES)
        self.assertEqual(definition.description, "A test tool")
        self.assertEqual(definition.inputSchema, {"type": "object", "properties": {}})

    def test_tool_definition_prompt_fields_default_empty(self):
        definition = ToolDefinition(
            name=ToolName.DISPLAY_USER_RESOURCES,
            description="A test tool",
            inputSchema={"type": "object", "properties": {}},
        )

        self.assertEqual(definition.usage_instructions, "")
        self.assertEqual(definition.workflow_instructions, "")

    def test_tool_definition_rejects_unknown_name(self):
        with self.assertRaises(ValidationError):
            ToolDefinition(
                name="bogus",
                description="x",
                inputSchema={"type": "object", "properties": {}},
            )


class ToolNameEnumTest(TestCase):
    def test_invalid_value_raises(self):
        with self.assertRaises(ValueError):
            ToolName("bogus")


class ToolRegistryTest(TestCase):
    def test_show_user_resources_is_registered(self):
        self.assertIn(ToolName.DISPLAY_USER_RESOURCES, tool_registry)

    def test_registry_accepts_raw_string_lookup(self):
        # The LLM API returns plain strings — get/__contains__ normalise them.
        self.assertIn("display_user_resources", tool_registry)
        self.assertIsNotNone(tool_registry.get("display_user_resources"))

    def test_registry_returns_none_for_unknown_string(self):
        self.assertIsNone(tool_registry.get("bogus"))
        self.assertNotIn("bogus", tool_registry)

    def test_show_user_resources_has_correct_definition(self):
        tool = tool_registry.get(ToolName.DISPLAY_USER_RESOURCES)

        self.assertEqual(tool.name, ToolName.DISPLAY_USER_RESOURCES)
        self.assertIn("cloud resources", tool.definition.description.lower())
        self.assertEqual(tool.definition.inputSchema["type"], "object")
        self.assertIn("project_uuid", tool.definition.inputSchema["properties"])
        self.assertIn("customer_uuid", tool.definition.inputSchema["properties"])
        self.assertIn("category_uuid", tool.definition.inputSchema["properties"])
        self.assertIn("state", tool.definition.inputSchema["properties"])
        self.assertEqual(tool.definition.inputSchema["required"], [])

    def test_show_user_resources_has_usage_instructions(self):
        tool = tool_registry.get(ToolName.DISPLAY_USER_RESOURCES)
        self.assertNotEqual(tool.definition.usage_instructions, "")

    def test_get_openai_tools_no_filter_returns_all(self):
        all_tools = tool_registry.get_openai_tools()
        filtered = tool_registry.get_openai_tools(tool_names=None)
        self.assertEqual(len(all_tools), len(filtered))

    def test_get_openai_tools_with_tool_names_filter(self):
        tools = tool_registry.get_openai_tools(
            tool_names=[ToolName.DISPLAY_USER_RESOURCES]
        )
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["function"]["name"], "display_user_resources")

    def test_get_tools_prompt_with_tool_names(self):
        # The registry's tool_names filter still governs which tools appear
        # in the catalog. usage_instructions are NOT inlined anymore — they
        # are fetched lazily via search_tools — so we assert on the catalog
        # instead.
        prompt = tool_registry.get_tools_prompt(
            tool_names=[ToolName.DISPLAY_USER_RESOURCES]
        )
        self.assertIn("TOOL CATALOG", prompt)
        self.assertIn("display_user_resources", prompt)


class GetToolsPromptTest(TestCase):
    def test_returns_string(self):
        prompt = tool_registry.get_tools_prompt()
        self.assertIsInstance(prompt, str)

    def test_includes_tool_names(self):
        # Tool names are now passed via the API tools param (get_openai_tools()), not in the prompt
        openai_tools = tool_registry.get_openai_tools()
        tool_names = [t["function"]["name"] for t in openai_tools]
        self.assertIn("display_user_resources", tool_names)

    def test_includes_tool_descriptions(self):
        # Tool descriptions are passed via the API tools param (get_openai_tools()), not in the prompt
        openai_tools = tool_registry.get_openai_tools()
        show_user_resources_tool = next(
            t for t in openai_tools if t["function"]["name"] == "display_user_resources"
        )
        self.assertTrue(len(show_user_resources_tool["function"]["description"]) > 0)

    def test_usage_instructions_available_via_registry(self):
        # usage_instructions used to be inlined in the system prompt. They
        # are now fetched via search_tools, so the assertion shifts from
        # "in prompt" to "accessible via the registry" — the search_tools
        # response copies this field verbatim.
        tool = tool_registry.get(ToolName.DISPLAY_USER_RESOURCES)
        self.assertTrue(tool.definition.usage_instructions.strip())

    def test_tool_with_parameters_shows_parameter_info(self):
        # create_vm has a rich inputSchema — verify get_openai_tools() emits it.
        openai_tools = tool_registry.get_openai_tools()
        create_vm_tool = next(
            t for t in openai_tools if t["function"]["name"] == "create_vm"
        )
        params = create_vm_tool["function"]["parameters"]
        self.assertIn("project_uuid", params["properties"])
        self.assertEqual(params["properties"]["project_uuid"]["type"], "string")
        self.assertIn("project_name", params["properties"])
        self.assertIn("name", params["properties"])
        # project_uuid/project_name at-least-one is enforced in execute(),
        # not via the JSON Schema `required` list.
        self.assertNotIn("project_uuid", params["required"])
        self.assertNotIn("project_name", params["required"])
        self.assertIn("name", params["required"])

    def test_prompt_has_available_tools_section(self):
        # AVAILABLE TOOLS section was moved to API tools param; it should NOT be in the prompt
        prompt = tool_registry.get_tools_prompt()
        self.assertNotIn("AVAILABLE TOOLS", prompt)

    def test_prompt_has_tool_catalog_section(self):
        # The lazy-load architecture replaced "TOOL USAGE GUIDELINES" with
        # a name+category catalog that the LLM uses to pick which
        # search_tools category to request.
        prompt = tool_registry.get_tools_prompt()
        self.assertIn("TOOL CATALOG", prompt)


class SystemPromptTest(TestCase):
    def test_system_prompt_template_is_string(self):
        self.assertIsInstance(SYSTEM_PROMPT_TEMPLATE, str)

    def test_system_prompt_template_has_persona(self):
        self.assertIn(PERSONA_TEMPLATE, SYSTEM_PROMPT_TEMPLATE)

    def test_system_prompt_template_has_placeholders(self):
        self.assertIn("{tools}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{scope_boundary}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{assistant_name}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{organization}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("{currency}", SYSTEM_PROMPT_TEMPLATE)

    def test_system_prompt_template_formats_correctly(self):
        result = SYSTEM_PROMPT_TEMPLATE.format(
            scope_boundary="[scope boundary]",
            tools="[tool prompt]",
            assistant_name="TestBot",
            organization="TestOrg",
            currency="EUR",
            custom_instructions="[custom instructions]",
        )
        self.assertIn("TestBot", result)
        self.assertIn("TestOrg", result)
        self.assertIn("EUR", result)
        self.assertIn("[tool prompt]", result)
        self.assertIn("[scope boundary]", result)
        self.assertIn("[custom instructions]", result)
        self.assertNotIn("{assistant_name}", result)
        self.assertNotIn("{organization}", result)
        self.assertNotIn("{currency}", result)
        self.assertNotIn("{tools}", result)
        self.assertNotIn("{scope_boundary}", result)
        self.assertNotIn("{custom_instructions}", result)


class ToolSetsTest(TestCase):
    def test_tool_sets_hierarchy(self):
        # Anonymous ⊆ end-user ⊆ support ⊆ staff: each tier sees a strict
        # superset of the prior. Anonymous baseline is the public marketplace
        # surface; everyone above sees that plus more.
        self.assertTrue(set(ANONYMOUS_TOOLS) <= set(END_USER_TOOLS))
        self.assertTrue(set(END_USER_TOOLS) <= set(SUPPORT_TOOLS))
        self.assertTrue(set(SUPPORT_TOOLS) <= set(STAFF_TOOLS))

    def test_tool_sets_all_exist_in_registry(self):
        all_names = (
            set(STAFF_TOOLS)
            | set(SUPPORT_TOOLS)
            | set(END_USER_TOOLS)
            | set(ANONYMOUS_TOOLS)
        )
        for name in all_names:
            self.assertIn(name, tool_registry, f"Tool {name!r} not found in registry")

    def test_tool_sets_contain_enum_members(self):
        for tool_set in (STAFF_TOOLS, SUPPORT_TOOLS, END_USER_TOOLS, ANONYMOUS_TOOLS):
            self.assertTrue(all(isinstance(t, ToolName) for t in tool_set))

    def test_meta_tools_in_every_authenticated_role_set(self):
        # search_tools is the lazy-loading meta-tool used by authenticated
        # paths. Anonymous deliberately does NOT include it — the anon
        # endpoint exposes a fixed tool set up-front instead.
        for tool_set in (STAFF_TOOLS, SUPPORT_TOOLS, END_USER_TOOLS):
            self.assertIn(ToolName.SEARCH_TOOLS, tool_set)
            self.assertIn(ToolName.ASK_USER, tool_set)
        self.assertNotIn(ToolName.SEARCH_TOOLS, ANONYMOUS_TOOLS)
        self.assertIn(ToolName.ASK_USER, ANONYMOUS_TOOLS)

    def test_anonymous_tools_is_marketplace_plus_ask_user(self):
        # Anonymous surface is intentionally narrow: marketplace browsing
        # tools + ask_user, nothing else. Adding tools here is a security
        # decision — every entry exposes more surface to unauthenticated
        # callers.
        self.assertEqual(
            set(ANONYMOUS_TOOLS),
            set(_MARKETPLACE_TOOLS) | {ToolName.ASK_USER},
        )


class GetToolSetForUserTest(TestCase):
    def test_none_returns_anonymous_tools(self):
        self.assertEqual(get_tool_set_for_user(None), ANONYMOUS_TOOLS)

    def test_anonymous_user_returns_anonymous_tools(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(get_tool_set_for_user(AnonymousUser()), ANONYMOUS_TOOLS)

    def test_staff_returns_staff_tools(self):
        user = MagicMock(is_staff=True, is_support=False, is_anonymous=False)
        self.assertEqual(get_tool_set_for_user(user), STAFF_TOOLS)

    def test_support_returns_support_tools(self):
        user = MagicMock(is_staff=False, is_support=True, is_anonymous=False)
        self.assertEqual(get_tool_set_for_user(user), SUPPORT_TOOLS)

    def test_end_user_returns_end_user_tools(self):
        user = MagicMock(is_staff=False, is_support=False, is_anonymous=False)
        self.assertEqual(get_tool_set_for_user(user), END_USER_TOOLS)


class ToolCategoryEnumTest(TestCase):
    def test_members_and_values(self):
        self.assertEqual(ToolCategory.VM.value, "vm")
        self.assertEqual(ToolCategory.MARKETPLACE.value, "marketplace")
        self.assertEqual(
            ToolCategory.PROPOSALS_RESEARCHER.value, "proposals_researcher"
        )
        self.assertEqual(ToolCategory.PROPOSALS_REVIEWER.value, "proposals_reviewer")

    def test_is_string_enum(self):
        self.assertIsInstance(ToolCategory.VM.value, str)


class ToolDefinitionCategoryFieldTest(TestCase):
    def _build(self, **overrides):
        kwargs = dict(
            name=ToolName.PLAN_VM,
            description="desc",
            inputSchema={"type": "object", "properties": {}},
        )
        kwargs.update(overrides)
        return ToolDefinition(**kwargs)

    def test_category_defaults_to_none(self):
        td = self._build()
        self.assertIsNone(td.category)

    def test_category_accepts_enum_member(self):
        td = self._build(category=ToolCategory.VM)
        self.assertEqual(td.category, ToolCategory.VM)

    def test_category_coerces_from_string(self):
        td = self._build(category="vm")
        self.assertEqual(td.category, ToolCategory.VM)


class VMToolsHaveVMCategoryTest(TestCase):
    def test_all_vm_tools_declare_vm_category(self):
        vm_tool_names = {
            ToolName.CREATE_VM,
            ToolName.PLAN_VM,
            ToolName.PLAN_VM,
        }
        for name in vm_tool_names:
            tool = tool_registry.get(name)
            self.assertIsNotNone(tool, f"{name} not registered")
            self.assertEqual(
                tool.definition.category,
                ToolCategory.VM,
                f"{name} should declare category=VM",
            )


class AccountToolsHaveAccountCategoryTest(TestCase):
    def test_all_account_tools_declare_account_category(self):
        account_tool_names = {
            ToolName.DISPLAY_USER_RESOURCES,
        }
        for name in account_tool_names:
            tool = tool_registry.get(name)
            self.assertIsNotNone(tool, f"{name} not registered")
            self.assertEqual(
                tool.definition.category,
                ToolCategory.ACCOUNT,
                f"{name} should declare category=ACCOUNT",
            )


class MarketplaceToolsHaveMarketplaceCategoryTest(TestCase):
    def test_all_marketplace_tools_declare_marketplace_category(self):
        marketplace_tool_names = {
            ToolName.SEARCH_OFFERINGS,
            ToolName.GET_OFFERING,
            ToolName.LIST_CATEGORIES,
            ToolName.COMPARE_OFFERINGS,
        }
        for name in marketplace_tool_names:
            tool = tool_registry.get(name)
            self.assertIsNotNone(tool, f"{name} not registered")
            self.assertEqual(
                tool.definition.category,
                ToolCategory.MARKETPLACE,
                f"{name} should declare category=MARKETPLACE",
            )


class ProposalsResearcherToolsHaveCategoryTest(TestCase):
    def test_all_researcher_tools_declare_researcher_category(self):
        names = {
            ToolName.FIND_MATCHING_CALLS,
            ToolName.GUIDE_PROPOSAL,
            ToolName.PROPOSAL_OVERVIEW,
        }
        for name in names:
            tool = tool_registry.get(name)
            self.assertIsNotNone(tool, f"{name} not registered")
            self.assertEqual(
                tool.definition.category,
                ToolCategory.PROPOSALS_RESEARCHER,
                f"{name} should declare category=PROPOSALS_RESEARCHER",
            )


class ProposalsReviewerToolsHaveCategoryTest(TestCase):
    def test_all_reviewer_tools_declare_reviewer_category(self):
        names = {
            ToolName.REVIEW_WORKLOAD,
            ToolName.REVIEW_ASSISTANT,
            ToolName.CALL_INSIGHTS,
        }
        for name in names:
            tool = tool_registry.get(name)
            self.assertIsNotNone(tool, f"{name} not registered")
            self.assertEqual(
                tool.definition.category,
                ToolCategory.PROPOSALS_REVIEWER,
                f"{name} should declare category=PROPOSALS_REVIEWER",
            )


class ToolsByCategoryTest(TestCase):
    def test_returns_marketplace_tools(self):
        tools = tool_registry.tools_by_category(ToolCategory.MARKETPLACE)
        names = {t.definition.name for t in tools}
        self.assertEqual(
            names,
            {
                ToolName.SEARCH_OFFERINGS,
                ToolName.GET_OFFERING,
                ToolName.LIST_CATEGORIES,
                ToolName.COMPARE_OFFERINGS,
            },
        )

    def test_returns_vm_tools(self):
        tools = tool_registry.tools_by_category(ToolCategory.VM)
        names = {t.definition.name for t in tools}
        self.assertEqual(
            names,
            {
                ToolName.CREATE_VM,
                ToolName.PLAN_VM,
                ToolName.PLAN_VM,
            },
        )

    def test_returns_account_tools(self):
        tools = tool_registry.tools_by_category(ToolCategory.ACCOUNT)
        names = {t.definition.name for t in tools}
        self.assertEqual(
            names,
            {
                ToolName.DISPLAY_USER_RESOURCES,
                ToolName.LIST_ORGANIZATIONS,
                ToolName.LIST_PROJECTS,
                ToolName.GET_PROJECT_RESOURCES,
                ToolName.GET_PROJECT_QUOTA,
                ToolName.GET_RESOURCE_USAGE,
                ToolName.GET_USER_OVERVIEW,
                ToolName.EXPLAIN_PROJECT_CREDIT_BALANCE,
                ToolName.LIST_OVERDRAWN_PROJECTS,
                ToolName.EXPLAIN_RESOURCE_PAUSED_REASON,
                ToolName.EXPLAIN_INVOICE_COMPENSATIONS,
                ToolName.GET_CUSTOMER_CREDIT_OVERVIEW,
            },
        )

    def test_excludes_search_tools(self):
        for cat in ToolCategory:
            tools = tool_registry.tools_by_category(cat)
            for t in tools:
                self.assertNotEqual(t.definition.name, ToolName.SEARCH_TOOLS)

    def test_excludes_ask_user(self):
        # ask_user is a meta-tool (category=None), so it must not appear in
        # any category bucket — same contract as search_tools.
        for cat in ToolCategory:
            tools = tool_registry.tools_by_category(cat)
            for t in tools:
                self.assertNotEqual(t.definition.name, ToolName.ASK_USER)


class RegistrationCategoryCheckTest(TestCase):
    def _make_tool(self, name, category):
        class _T(BaseTool):
            @property
            def definition(self_inner):
                return ToolDefinition(
                    name=name,
                    category=category,
                    description="t",
                    inputSchema={"type": "object", "properties": {}},
                )

            def execute(self_inner, user, arguments):
                return {"type": "success", "summary": "ok"}

        return _T()

    def test_rejects_non_meta_tool_without_category(self):
        reg = ToolRegistry()
        tool = self._make_tool(ToolName.PLAN_VM, None)
        with self.assertRaises(ValueError) as ctx:
            reg.register(tool)
        self.assertIn("category", str(ctx.exception).lower())

    def test_accepts_search_tools_without_category(self):
        reg = ToolRegistry()
        tool = self._make_tool(ToolName.SEARCH_TOOLS, None)
        reg.register(tool)
        self.assertIn(ToolName.SEARCH_TOOLS, reg)

    def test_accepts_ask_user_without_category(self):
        # ask_user is the second meta-tool; the registration allowlist must
        # cover it too, otherwise the always-loaded primitive can't even
        # come up at app start.
        reg = ToolRegistry()
        tool = self._make_tool(ToolName.ASK_USER, None)
        reg.register(tool)
        self.assertIn(ToolName.ASK_USER, reg)

    def test_accepts_tool_with_category(self):
        reg = ToolRegistry()
        tool = self._make_tool(ToolName.PLAN_VM, ToolCategory.VM)
        reg.register(tool)
        self.assertIn(ToolName.PLAN_VM, reg)


class SearchToolsCategoriesTest(TestCase):
    def setUp(self):
        self.tool = tool_registry.get(ToolName.SEARCH_TOOLS)
        self.assertIsNotNone(self.tool)

    def test_input_schema_declares_categories(self):
        schema = self.tool.definition.inputSchema
        self.assertIn("categories", schema["properties"])
        self.assertEqual(schema["required"], ["categories"])
        cats_schema = schema["properties"]["categories"]
        self.assertEqual(cats_schema["type"], "array")
        self.assertEqual(cats_schema["minItems"], 1)
        self.assertEqual(
            set(cats_schema["items"]["enum"]),
            {c.value for c in ToolCategory},
        )

    def test_input_schema_no_longer_accepts_tool_names(self):
        schema = self.tool.definition.inputSchema
        self.assertNotIn("tool_names", schema["properties"])

    def test_execute_loads_whole_category(self):
        result = self.tool.execute(user=None, arguments={"categories": ["marketplace"]})
        self.assertEqual(result["type"], "success")
        names = set(result["data"]["fetched_names"])
        self.assertEqual(
            names,
            {
                "search_offerings",
                "get_offering",
                "list_categories",
                "compare_offerings",
            },
        )

    def test_execute_multiple_categories_deduped(self):
        # Use a staff user — vm tools aren't in ANONYMOUS_TOOLS, so passing
        # user=None would correctly filter them out and break this test's
        # premise (which is dedup, not permission filtering).
        staff = MagicMock(is_staff=True, is_support=False, is_anonymous=False)
        result = self.tool.execute(
            user=staff,
            arguments={"categories": ["marketplace", "marketplace", "vm"]},
        )
        self.assertEqual(result["type"], "success")
        names = set(result["data"]["fetched_names"])
        # 4 marketplace + 2 vm tools (create_vm, plan_vm). display_user_resources
        # is registered as account-category, not vm.
        self.assertEqual(len(names), 6)
        self.assertIn("search_offerings", names)
        self.assertIn("plan_vm", names)

    def test_execute_unknown_category_reported_in_missing(self):
        result = self.tool.execute(
            user=None,
            arguments={"categories": ["not_a_category", "marketplace"]},
        )
        self.assertEqual(result["type"], "success")
        self.assertIn("not_a_category", result["data"]["missing"])
        self.assertIn("search_offerings", result["data"]["fetched_names"])

    def test_execute_all_unknown_returns_error(self):
        result = self.tool.execute(user=None, arguments={"categories": ["nope"]})
        self.assertEqual(result["type"], "error")

    def test_execute_never_returns_search_tools_itself(self):
        for cat in ToolCategory:
            result = self.tool.execute(user=None, arguments={"categories": [cat.value]})
            if result["type"] == "success":
                self.assertNotIn("search_tools", result["data"]["fetched_names"])


class SearchToolsPermissionFilterTest(TestCase):
    def setUp(self):
        self.tool = tool_registry.get(ToolName.SEARCH_TOOLS)

    def _end_user(self):
        u = MagicMock()
        u.is_staff = False
        u.is_support = False
        # Explicit: must NOT be anonymous, otherwise get_tool_set_for_user
        # returns ANONYMOUS_TOOLS instead of END_USER_TOOLS.
        u.is_anonymous = False
        return u

    def test_end_user_still_gets_permitted_reviewer_tools(self):
        # END_USER_TOOLS includes the reviewer tools today (see tool_sets).
        # This test asserts that permission-filtering does not strip tools
        # the user IS allowed to see.
        result = self.tool.execute(
            user=self._end_user(),
            arguments={"categories": ["proposals_reviewer"]},
        )
        self.assertEqual(result["type"], "success")
        fetched = set(result["data"]["fetched_names"])
        # End users in the current tool_sets only have REVIEW_WORKLOAD,
        # REVIEW_ASSISTANT, PROPOSAL_OVERVIEW, REVIEW_ASSISTANT —
        # CALL_INSIGHTS is staff-only.
        self.assertIn("review_workload", fetched)
        self.assertIn("review_assistant", fetched)
        self.assertNotIn("call_insights", fetched)
        self.assertEqual(result["data"]["empty"], [])

    def test_category_with_no_permitted_tools_reported_in_empty(self):
        marketplace_names = {
            ToolName.SEARCH_OFFERINGS,
            ToolName.GET_OFFERING,
            ToolName.LIST_CATEGORIES,
            ToolName.COMPARE_OFFERINGS,
        }
        restricted = [t for t in tool_sets.STAFF_TOOLS if t not in marketplace_names]
        with patch(
            "waldur_mastermind.chat.tools.search_tools.get_tool_set_for_user",
            return_value=restricted,
        ):
            result = self.tool.execute(
                user=MagicMock(),
                arguments={"categories": ["marketplace", "vm"]},
            )

        self.assertEqual(result["type"], "success")
        self.assertIn("marketplace", result["data"]["empty"])
        self.assertIn("plan_vm", result["data"]["fetched_names"])

    def test_all_requested_categories_empty_returns_error(self):
        with patch(
            "waldur_mastermind.chat.tools.search_tools.get_tool_set_for_user",
            return_value=[ToolName.SEARCH_TOOLS],
        ):
            result = self.tool.execute(
                user=MagicMock(),
                arguments={"categories": ["marketplace"]},
            )

        self.assertEqual(result["type"], "error")
        self.assertIn("marketplace", result["data"]["empty"])


class CatalogGroupedByCategoryTest(TestCase):
    def test_catalog_has_category_headers_in_fixed_order(self):
        prompt = tool_registry.get_tools_prompt()
        idx_market = prompt.find("## marketplace")
        idx_vm = prompt.find("## vm")
        idx_res = prompt.find("## proposals_researcher")
        idx_rev = prompt.find("## proposals_reviewer")
        self.assertTrue(
            0 <= idx_market < idx_vm < idx_res < idx_rev,
            f"Catalog ordering wrong: market={idx_market} vm={idx_vm} "
            f"researcher={idx_res} reviewer={idx_rev}\nprompt={prompt}",
        )

    def test_catalog_excludes_search_tools(self):
        # The catalog section lists tools grouped by `## category` headers.
        # `search_tools` is mentioned in the calling-contract preamble above
        # the catalog, but must not appear under any category header.
        prompt = tool_registry.get_tools_prompt()
        catalog_section = prompt.split("=== TOOL CATALOG ===", 1)[1]
        self.assertNotIn("`search_tools`", catalog_section)

    def test_catalog_excludes_ask_user_from_category_listings(self):
        # Meta-tool — must not appear under any ## category header.
        prompt = tool_registry.get_tools_prompt()
        for cat in ToolCategory:
            start = prompt.find(f"## {cat.value}")
            if start < 0:
                continue
            end = prompt.find("\n## ", start + 1)
            block = prompt[start:end] if end > 0 else prompt[start:]
            self.assertNotIn("`ask_user`", block)

    def test_contract_preamble_mentions_ask_user_as_always_available(self):
        prompt = tool_registry.get_tools_prompt()
        contract_section = prompt.split("=== TOOL CATALOG ===", 1)[0]
        self.assertIn("ask_user", contract_section)
        self.assertIn("ALWAYS available", contract_section)

    def test_catalog_lists_tools_under_their_category(self):
        prompt = tool_registry.get_tools_prompt()
        start = prompt.find("## marketplace")
        end = prompt.find("\n## ", start + 1)
        marketplace_block = prompt[start:end] if end > 0 else prompt[start:]
        self.assertIn("`search_offerings`", marketplace_block)
        self.assertNotIn("`create_vm`", marketplace_block)


class AccountToolEnumTest(TestCase):
    def test_list_organizations_enum(self):
        self.assertEqual(ToolName.LIST_ORGANIZATIONS.value, "list_organizations")

    def test_list_projects_enum_stays_stable(self):
        # Enum value stays the same — downstream LLM tool-call logs still
        # resolve after the tool is repointed to the new general tool.
        self.assertEqual(ToolName.LIST_PROJECTS.value, "list_projects")

    def test_plan_vm_enum(self):
        self.assertEqual(
            ToolName.PLAN_VM.value,
            "plan_vm",
        )

    def test_get_project_resources_enum(self):
        self.assertEqual(ToolName.GET_PROJECT_RESOURCES.value, "get_project_resources")

    def test_get_project_quota_enum(self):
        self.assertEqual(ToolName.GET_PROJECT_QUOTA.value, "get_project_quota")

    def test_get_resource_usage_enum(self):
        self.assertEqual(ToolName.GET_RESOURCE_USAGE.value, "get_resource_usage")

    def test_get_user_overview_enum(self):
        self.assertEqual(ToolName.GET_USER_OVERVIEW.value, "get_user_overview")

    def test_display_user_resources_enum(self):
        # Renamed from SHOW_USER_RESOURCES — value changes too.
        self.assertEqual(
            ToolName.DISPLAY_USER_RESOURCES.value, "display_user_resources"
        )
        self.assertFalse(hasattr(ToolName, "SHOW_USER_RESOURCES"))

    def test_account_category_enum(self):
        self.assertEqual(ToolCategory.ACCOUNT.value, "account")
