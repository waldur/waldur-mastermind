"""A delegated caller is offered only the tools it may run.

``get_tool_set_for_user`` leaves the HPC tools out for a delegated caller (a
personal access token, an identity provider token or impersonation). The
prompt's tool catalog, ``search_tools`` and the canned rejection have to
agree with the executor, or the model is offered tools it is then refused.
"""

import re
from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.structure.tests import factories as structure_factories
from waldur_firecrest.tests.test_hpc_tools import API_URL, HpcToolsTestBase
from waldur_mastermind.chat.context_assembler import build_context
from waldur_mastermind.chat.prompts.rejection import (
    CATEGORY_PHRASES,
    build_canned_rejection,
)
from waldur_mastermind.chat.tests.utils import (
    _make_content_chunk,
    _mock_openai_client,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.tool_sets import _META_TOOLS

HPC_PHRASE = CATEGORY_PHRASES[ToolCategory.HPC]
CATALOG_ENTRY = re.compile(r"^- `([a-z_]+)`:", re.MULTILINE)


def system_prompt(user, delegated):
    messages = build_context(user, "hello", delegated=delegated)
    return next(m["content"] for m in messages if m["role"] == "system")


def catalog_tools(user, delegated):
    prompt = system_prompt(user, delegated)
    catalog = prompt.split("=== TOOL CATALOG ===", 1)[1].split("\n===", 1)[0]
    return set(CATALOG_ENTRY.findall(catalog))


def search_tools_names(executor, categories):
    result = executor.execute_tool(
        ToolName.SEARCH_TOOLS.value, {"categories": categories}
    )
    if result["type"] != "success":
        return set()
    return set(result["data"]["fetched_names"])


@override_config(FIRECREST_ALLOWED_API_URLS=API_URL)
class DelegatedToolSetTest(HpcToolsTestBase):
    def test_catalog_has_no_hpc_category_for_a_delegated_caller(self):
        self.assertIn("## hpc", system_prompt(self.user, delegated=False))
        self.assertNotIn("## hpc", system_prompt(self.user, delegated=True))

    def test_search_tools_loads_no_hpc_tool_for_a_delegated_caller(self):
        direct = search_tools_names(ToolExecutor(self.user), ["hpc"])
        self.assertIn(ToolName.SUBMIT_HPC_JOB.value, direct)

        result = ToolExecutor(self.user, delegated=True).execute_tool(
            ToolName.SEARCH_TOOLS.value, {"categories": ["hpc"]}
        )
        self.assertEqual(result["type"], "error")
        self.assertEqual(result["data"]["empty"], ["hpc"])

    def test_canned_rejection_offers_no_hpc_to_a_delegated_caller(self):
        self.assertIn(HPC_PHRASE, build_canned_rejection(self.user, "MyOrg"))
        self.assertNotIn(
            HPC_PHRASE, build_canned_rejection(self.user, "MyOrg", delegated=True)
        )

    def test_catalog_search_tools_and_executor_agree(self):
        support = structure_factories.UserFactory(is_support=True)
        all_categories = [c.value for c in ToolCategory]
        meta = {tool.value for tool in _META_TOOLS}
        for user in (self.user, support, self.fixture.staff):
            for delegated in (False, True):
                with self.subTest(user=user.username, delegated=delegated):
                    executor = ToolExecutor(user, delegated=delegated)
                    runnable = {tool.value for tool in executor.permitted} - meta
                    self.assertEqual(catalog_tools(user, delegated), runnable)
                    self.assertEqual(
                        search_tools_names(executor, all_categories), runnable
                    )


@override_config(
    FIRECREST_ALLOWED_API_URLS=API_URL,
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class DelegatedStreamTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.url = reverse("chat-stream")

    def sent_system_prompt(self, mock_openai_cls):
        mock_openai_cls.return_value = _mock_openai_client([_make_content_chunk("Hi")])
        response = self.client.post(self.url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        list(response.streaming_content)
        create = mock_openai_cls.return_value.chat.completions.create
        messages = create.call_args_list[0].kwargs["messages"]
        return next(m["content"] for m in messages if m["role"] == "system")

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_own_sign_in_gets_the_hpc_catalog(self, mock_openai_cls):
        self.client.force_authenticate(user=self.user)
        self.assertIn("## hpc", self.sent_system_prompt(mock_openai_cls))

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    def test_impersonated_user_gets_no_hpc_catalog(self, mock_openai_cls):
        self.user.impersonator = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)
        self.assertNotIn("## hpc", self.sent_system_prompt(mock_openai_cls))
