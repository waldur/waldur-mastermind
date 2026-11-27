from unittest import mock

from django.test import SimpleTestCase

from waldur_firecrest import workspace as firecrest_workspace
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat import hooks as chat_hooks
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry


class ChatIntegrationHooksTest(SimpleTestCase):
    def test_firecrest_and_openstack_chat_tools_are_registered(self):
        for tool_name in (
            ToolName.CREATE_VM,
            ToolName.PLAN_VM,
            ToolName.LIST_HPC_ALLOCATIONS,
            ToolName.GET_HPC_CLUSTER_STATUS,
            ToolName.FIND_HPC_SOFTWARE,
            ToolName.PLAN_HPC_JOB,
            ToolName.SUBMIT_HPC_JOB,
            ToolName.LIST_HPC_JOBS,
            ToolName.GET_HPC_JOB,
            ToolName.CANCEL_HPC_JOB,
            ToolName.GET_HPC_ALLOCATION_USAGE,
            ToolName.GET_HPC_JOB_STATS,
            ToolName.GET_HPC_CLUSTER_LOAD,
            ToolName.PLOT_HPC_OUTPUT,
        ):
            self.assertIn(tool_name, tool_registry)

    def test_hpc_workspace_provider_delegates_and_respects_patches(self):
        self.assertIsNotNone(chat_hooks.get_hpc_workspace_provider())

        with mock.patch.object(
            firecrest_workspace, "is_configured", return_value=True
        ) as configured_mock:
            self.assertTrue(chat_hooks.is_hpc_configured())
            self.assertTrue(deployment_profile.CAPABILITIES["hpc"]())
            self.assertEqual(configured_mock.call_count, 2)

        sentinel_user = object()
        sentinel_qs = object()
        with mock.patch.object(
            firecrest_workspace, "resources_for_user", return_value=sentinel_qs
        ) as resources_mock:
            self.assertIs(
                chat_hooks.get_hpc_resources_for_user(sentinel_user), sentinel_qs
            )
            resources_mock.assert_called_once_with(sentinel_user)

    def test_hpc_workspace_provider_fallbacks_when_unregistered(self):
        with mock.patch.object(chat_hooks, "_hpc_workspace_provider", None):
            self.assertFalse(chat_hooks.is_hpc_configured())
            self.assertFalse(deployment_profile.CAPABILITIES["hpc"]())
            self.assertIsNone(chat_hooks.get_hpc_resources_for_user(object()))
