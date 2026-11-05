import json
import time

import jwt
import responses
from django.core.cache import cache
from django.test import TestCase

from waldur_auth_social import oauth_tokens
from waldur_core.configuration.testing import override_config
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_firecrest.tests.test_monitor_only import accounting, job_row
from waldur_firecrest.workspace import identity_cache_key
from waldur_mastermind.chat.block_schemas import BlockSerializer
from waldur_mastermind.chat.llm_streamer import LLMStreamer
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.hpc.cluster import (
    GetHpcClusterStatusTool,
    ListHpcAllocationsTool,
)
from waldur_mastermind.chat.tools.hpc.insights import (
    GetHpcAllocationUsageTool,
    GetHpcClusterLoadTool,
    GetHpcJobStatsTool,
    PlotHpcOutputTool,
)
from waldur_mastermind.chat.tools.hpc.jobs import (
    CARD_SHOWS_SCRIPT,
    CancelHpcJobTool,
    GetHpcJobTool,
    PlanHpcJobTool,
    SubmitHpcJobTool,
)
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    _HPC_TOOLS,
    END_USER_TOOLS,
    get_tool_set_for_user,
)
from waldur_mastermind.chat.ui_registry import ui_registry
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories

API_URL = "http://firecrest:5000"
MONITOR_URL = "http://slurm-monitor:12000/api/v2"
SYSTEM = "slurm-a"
ACCOUNT = "alloc_1"


@override_config(FIRECREST_ALLOWED_API_URLS=API_URL)
class HpcToolsTestBase(TestCase):
    def setUp(self):
        self.fixture = ProjectFixture()
        self.user = self.fixture.member
        self.offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            plugin_options={
                "firecrest_api_url": API_URL,
                "firecrest_system_name": SYSTEM,
            },
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
            backend_id=ACCOUNT,
            name="climate-sim",
        )
        factories.OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="hpc_9000",
            state=OfferingUserStates.OK,
        )
        cache.clear()
        # FirecREST's view of the user agrees; IdentityTest covers the check.
        cache.set(identity_cache_key(self.user, self.offering, ACCOUNT), True)
        factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="batch", max_time=120, max_nodes=4
        )
        token = jwt.encode(
            {"exp": int(time.time()) + 3600}, "k" * 32, algorithm="HS256"
        )
        oauth_tokens.cache_access_token(self.user, token)
        self.job_args = {
            "name": "sim",
            "commands": "srun ./model",
            "partition": "batch",
            "nodes": 2,
            "time_limit_minutes": 60,
        }


@override_config(FIRECREST_ALLOWED_API_URLS=API_URL)
class HpcToolsTest(HpcToolsTestBase):
    def test_delegated_callers_get_no_hpc_tools(self):
        # A personal access token, an identity provider token or impersonation
        # would act on the cluster as the user, with nothing to scope it down.
        tools = get_tool_set_for_user(self.user, delegated=True)
        self.assertNotIn(ToolName.SUBMIT_HPC_JOB, tools)
        self.assertNotIn(ToolName.LIST_HPC_ALLOCATIONS, tools)
        self.assertIn(ToolName.SUBMIT_HPC_JOB, get_tool_set_for_user(self.user))
        result = ToolExecutor(self.user, delegated=True).execute_tool(
            ToolName.SUBMIT_HPC_JOB.value, self.job_args
        )
        self.assertEqual(result["type"], "error")

    @override_config(FIRECREST_ALLOWED_API_URLS="", SLURM_MONITOR_ALLOWED_API_URLS="")
    def test_no_hpc_tools_without_an_allowed_endpoint(self):
        for user in (self.user, self.fixture.staff):
            self.assertTrue(set(get_tool_set_for_user(user)).isdisjoint(_HPC_TOOLS))
        self.assertIn(ToolName.LIST_PROJECTS, get_tool_set_for_user(self.user))
        result = ToolExecutor(self.user).execute_tool(
            ToolName.LIST_HPC_ALLOCATIONS.value, {}
        )
        self.assertEqual(result["type"], "error")

    @override_config(
        FIRECREST_ALLOWED_API_URLS="", SLURM_MONITOR_ALLOWED_API_URLS=MONITOR_URL
    )
    def test_a_cluster_monitor_alone_brings_the_hpc_tools(self):
        self.assertIn(ToolName.LIST_HPC_JOBS, get_tool_set_for_user(self.user))

    def test_tools_registered_for_end_users(self):
        for name in (
            ToolName.LIST_HPC_ALLOCATIONS,
            ToolName.PLAN_HPC_JOB,
            ToolName.SUBMIT_HPC_JOB,
            ToolName.GET_HPC_JOB,
            ToolName.GET_HPC_ALLOCATION_USAGE,
            ToolName.GET_HPC_JOB_STATS,
            ToolName.GET_HPC_CLUSTER_LOAD,
            ToolName.PLOT_HPC_OUTPUT,
        ):
            self.assertIn(name, tool_registry)
            self.assertIn(name, END_USER_TOOLS)
            self.assertEqual(
                tool_registry.get(name).definition.category, ToolCategory.HPC
            )

    def test_allocations_are_listed_for_member_only(self):
        result = ListHpcAllocationsTool().execute(self.user, {})
        self.assertEqual(result["data"]["allocations"][0]["account"], ACCOUNT)
        result = ListHpcAllocationsTool().execute(ProjectFixture().member, {})
        self.assertEqual(result["data"]["allocations"], [])

    @responses.activate
    def test_plan_renders_preview_card_that_passes_block_schema(self):
        result = PlanHpcJobTool().execute(
            self.user,
            {
                **self.job_args,
                "files": [{"name": "prog.py", "content": "print(1)\n"}],
            },
        )
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(result["ui_data"]["status"], "preview")
        self.assertIn(f"#SBATCH --account={ACCOUNT}", result["ui_data"]["script"])
        self.assertIn("cat > prog.py <<'EOF'", result["ui_data"]["script"])
        self.assertEqual(
            result["ui_data"]["files"], [{"name": "prog.py", "content": "print(1)\n"}]
        )
        content = ui_registry.create_content("hpc_job", result["ui_data"])
        data = dict(content.data)
        block = {
            "id": "b1",
            "key": "hpc_job",
            "status": "complete",
            "job_status": data.pop("status"),
            **data,
        }
        serializer = BlockSerializer(data=block)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    @responses.activate
    def test_plan_beyond_limits_reports_errors(self):
        result = PlanHpcJobTool().execute(
            self.user, {**self.job_args, "time_limit_minutes": 600}
        )
        self.assertEqual(result["type"], "validation_error")
        self.assertEqual(result["ui_data"]["status"], "error")
        self.assertTrue(result["ui_data"]["errors"])

    @responses.activate
    def test_model_is_told_the_card_shows_the_script_and_not_given_it(self):
        # A model that has the script pastes it into its reply as well.
        submit = responses.post(
            f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobId": 7}, status=201
        )
        results = [
            PlanHpcJobTool().execute(self.user, self.job_args),
            PlanHpcJobTool().execute(
                self.user, {**self.job_args, "time_limit_minutes": 600}
            ),
            SubmitHpcJobTool().execute(self.user, self.job_args),
        ]
        self.assertEqual(len(submit.calls), 1)
        for result in results:
            # The model reads the data when there is any, else the summary.
            seen = (
                json.dumps(result["data"]) if result.get("data") else result["summary"]
            )
            self.assertIn(CARD_SHOWS_SCRIPT, seen)
            self.assertNotIn("#SBATCH", seen)

    @responses.activate
    def test_plan_tells_the_model_what_is_wrong_with_a_file(self):
        result = PlanHpcJobTool().execute(
            self.user,
            {**self.job_args, "files": [{"name": "src/prog.py", "content": ""}]},
        )
        self.assertEqual(result["type"], "validation_error")
        self.assertIn("files[0].name: A plain file name", result["summary"])
        self.assertIn(
            "files[0].content: This field may not be blank.", result["summary"]
        )

    @responses.activate
    def test_plan_gives_the_model_the_job_it_rendered(self):
        result = PlanHpcJobTool().execute(
            self.user,
            {
                **self.job_args,
                "files": [{"name": "prog.py", "content": "print(1)\n"}],
            },
        )
        job = result["data"]["job"]
        self.assertEqual(job["account"], ACCOUNT)
        self.assertEqual(job["files"], ["prog.py"])
        self.assertEqual(
            job["cpu_hours_at_most"],
            round(
                job["nodes"]
                * job["ntasks_per_node"]
                * job["cpus_per_task"]
                * job["time_limit_minutes"]
                / 60,
                2,
            ),
        )

    @responses.activate
    def test_plan_resolves_allocation_by_name(self):
        result = PlanHpcJobTool().execute(
            self.user, {**self.job_args, "allocation": "climate"}
        )
        self.assertEqual(result["ui_data"]["allocation_uuid"], self.resource.uuid.hex)

    @responses.activate
    def test_submit_posts_job_to_firecrest(self):
        submit = responses.post(
            f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobId": 7}, status=201
        )
        result = SubmitHpcJobTool().execute(self.user, self.job_args)
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(result["data"]["job_id"], "7")
        job = json.loads(submit.calls[0].request.body)["job"]
        self.assertEqual(job["account"], ACCOUNT)
        self.assertEqual(job["partition"], "batch")

    @responses.activate
    def test_get_job_includes_error_output(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/7",
            json={
                "jobs": [
                    {
                        "jobId": "7",
                        "name": "sim",
                        "status": {"state": "FAILED", "exitCode": 127},
                        "time": {"elapsed": 1},
                        "account": ACCOUNT,
                        "workingDirectory": "/home/hpc_9000",
                    }
                ]
            },
        )
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/7/metadata",
            json={
                "jobs": [
                    {
                        "jobId": "7",
                        "script": "#!/bin/bash\n",
                        "standardOutput": "/home/hpc_9000/sim-7.out",
                        "standardError": "/home/hpc_9000/sim-7.err",
                    }
                ]
            },
        )
        responses.get(
            f"{API_URL}/filesystem/{SYSTEM}/ops/tail",
            json={"output": {"content": "./model: command not found\n"}},
        )
        result = GetHpcJobTool().execute(self.user, {"job_id": "7"})
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(result["data"]["job"]["exit_code"], 127)
        self.assertIn("command not found", result["data"]["standard_error"])

    def test_non_numeric_job_id_is_rejected(self):
        result = CancelHpcJobTool().execute(self.user, {"job_id": "7; rm"})
        self.assertEqual(result["type"], "validation_error")

    def test_user_without_allocation_gets_error(self):
        result = PlanHpcJobTool().execute(ProjectFixture().member, self.job_args)
        self.assertEqual(result["type"], "error")


def persisted_block(chunk: dict) -> dict:
    """The block LLMStreamer writes to Message.blocks for a tool result chunk."""
    streamer = LLMStreamer.__new__(LLMStreamer)
    streamer.accumulated_blocks = []
    streamer._current_block = None
    streamer.accumulated_warning = ""
    streamer._absorb_block(chunk)
    streamer._finalize_current_block()
    return streamer.accumulated_blocks[0]


def chart_block(result: dict) -> dict:
    content = ui_registry.create_content("chart", result["ui_data"])
    block = persisted_block(json.loads(json.dumps({"k": "chart", **content.data})))
    serializer = BlockSerializer(data=block)
    assert serializer.is_valid(), serializer.errors
    return block


@override_config(FIRECREST_ALLOWED_API_URLS=API_URL)
class HpcInsightToolsTest(HpcToolsTestBase):
    @responses.activate
    def test_job_stats_chart_by_view(self):
        now = int(time.time())
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            json={
                "jobs": [
                    {
                        "jobId": str(i),
                        "name": "wrf",
                        "status": {"state": "COMPLETED", "exitCode": 0},
                        "time": {
                            "submission": now - 7200,
                            "start": now - 7000,
                            "end": now - 3400,
                            "elapsed": 3600,
                            "limit": 48 * 3600,
                            "totalCpu": 3600 * 64,
                        },
                        "account": ACCOUNT,
                        "partition": "batch",
                        "allocationCpus": 128,
                    }
                    for i in range(1, 5)
                ]
            },
        )
        result = GetHpcJobStatsTool().execute(
            self.user, {"view": "time_limits", "days": 7}
        )
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(result["data"]["totals"]["completed"], 4)
        self.assertEqual(result["data"]["median_cpu_efficiency"], 0.5)
        self.assertEqual(result["data"]["suggestions"][0]["kind"], "time_limit")
        fit = result["data"]["time_limit_fit"]
        self.assertEqual(fit["median_used_percent"], 2)
        self.assertEqual(fit["by_name"][0]["name"], "wrf")
        block = chart_block(result)
        self.assertEqual(block["chart_type"], "scatter")
        self.assertIn("hpc-insights", block["link"]["url"])

    def test_job_stats_rejects_unknown_view(self):
        result = GetHpcJobStatsTool().execute(self.user, {"view": "pie"})
        self.assertEqual(result["type"], "validation_error")

    @responses.activate
    def test_cluster_load_node_map(self):
        responses.get(
            f"{API_URL}/status/{SYSTEM}/nodes",
            json={
                "nodes": [
                    {
                        "name": "n1",
                        "partitions": ["batch"],
                        "state": ["IDLE"],
                        "cpus": 4,
                    },
                    {
                        "name": "n2",
                        "partitions": ["batch"],
                        "state": ["DOWN"],
                        "cpus": 4,
                        "reason": "Not responding",
                    },
                ]
            },
        )
        responses.get(
            f"{API_URL}/status/{SYSTEM}/reservations", json={"reservations": []}
        )
        result = GetHpcClusterLoadTool().execute(self.user, {})
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(
            result["data"]["unavailable_nodes"][0]["reason"], "Not responding"
        )
        self.assertEqual(chart_block(result)["chart_type"], "heatmap")

    def test_allocation_usage_without_components(self):
        result = GetHpcAllocationUsageTool().execute(self.user, {})
        self.assertEqual(result["type"], "success")
        self.assertNotIn("ui_component", result)

    @responses.activate
    def test_plot_xvg(self):
        responses.get(
            f"{API_URL}/filesystem/{SYSTEM}/ops/view",
            json={"output": '@ s0 legend "Potential"\n0 -10\n1 -12\n'},
        )
        result = PlotHpcOutputTool().execute(
            self.user, {"path": "/home/hpc_9000/energy.xvg"}
        )
        self.assertEqual(result["type"], "success", result)
        self.assertEqual(result["data"]["summary"]["Potential"]["last"], -12)
        self.assertEqual(chart_block(result)["chart_type"], "line")

    def test_plot_requires_absolute_path(self):
        result = PlotHpcOutputTool().execute(self.user, {"path": "energy.xvg"})
        self.assertEqual(result["type"], "validation_error")

    @responses.activate
    def test_job_card_is_persisted_with_its_own_status(self):
        result = PlanHpcJobTool().execute(self.user, self.job_args)
        content = ui_registry.create_content("hpc_job", result["ui_data"])
        block = persisted_block({"k": "hpc_job", **content.data})
        self.assertEqual(block["key"], "hpc_job")
        self.assertEqual(block["status"], "complete")
        self.assertEqual(block["job_status"], "preview")
        self.assertIn("#SBATCH", block["script"])
        serializer = BlockSerializer(data=block)
        self.assertTrue(serializer.is_valid(), serializer.errors)


@override_config(
    FIRECREST_ALLOWED_API_URLS="", SLURM_MONITOR_ALLOWED_API_URLS=MONITOR_URL
)
class MonitorOnlyToolsTest(HpcToolsTestBase):
    """An allocation whose offering has only a cluster monitor, no FirecREST."""

    def setUp(self):
        super().setUp()
        self.offering.plugin_options = {
            "slurm_monitor_api_url": MONITOR_URL,
            "slurm_monitor_cluster_name": "cluster-a",
        }
        self.offering.save()

    def test_allocation_says_jobs_cannot_be_run(self):
        result = ListHpcAllocationsTool().execute(self.user, {})
        (allocation,) = result["data"]["allocations"]
        self.assertEqual(allocation["cluster"], "cluster-a")
        self.assertFalse(allocation["can_run_jobs"])

    def test_a_job_is_not_planned_where_it_cannot_be_submitted(self):
        result = PlanHpcJobTool().execute(self.user, self.job_args)
        self.assertEqual(result["type"], "error")
        self.assertIn("need FirecREST", result["summary"])

    def test_cluster_status_is_the_published_partitions(self):
        with responses.RequestsMock() as mocked:
            result = GetHpcClusterStatusTool().execute(self.user, {})
            self.assertEqual(len(mocked.calls), 0)
        self.assertEqual(result["type"], "success", result)
        self.assertIn("partition(s) on cluster-a", result["summary"])
        self.assertIn("need FirecREST", result["summary"])
        self.assertNotIn("health", result["data"])
        (partition,) = result["data"]["partitions"]
        self.assertEqual(partition["name"], "batch")
        self.assertEqual(partition["max_time"], 120)
        self.assertFalse(partition["live"])

    @responses.activate
    def test_job_comes_from_the_monitor_without_its_files(self):
        monitor = f"{MONITOR_URL}/cluster/cluster-a"
        row = {"account": ACCOUNT, "user": "hpc_9000"}
        responses.get(f"{monitor}/jobs/11", json=job_row(11, **row))
        responses.get(
            f"{monitor}/jobs", json={"jobs": [job_row(11, **row, **accounting())]}
        )

        result = GetHpcJobTool().execute(self.user, {"job_id": "11"})

        self.assertEqual(result["type"], "success", result)
        self.assertIn("cannot be read without FirecREST", result["summary"])
        job = result["data"]["job"]
        self.assertEqual(job["job_id"], "11")
        self.assertEqual(job["state"], "COMPLETED")
        self.assertEqual(job["time_limit_minutes"], 120)
        self.assertEqual(job["cpu_time_seconds"], 28800)
        self.assertNotIn("standard_output", result["data"])

    @responses.activate
    def test_job_of_another_user_is_not_found(self):
        monitor = f"{MONITOR_URL}/cluster/cluster-a"
        responses.get(
            f"{monitor}/jobs/12", json=job_row(12, account=ACCOUNT, user="someone_else")
        )
        responses.get(f"{monitor}/jobs", json={"jobs": []})
        result = GetHpcJobTool().execute(self.user, {"job_id": "12"})
        self.assertEqual(result["type"], "error")
        self.assertIn("not found", result["summary"])

    @responses.activate
    def test_job_the_monitor_does_not_show_is_not_found(self):
        # slurm-monitor answers 404 for another user's job as for a missing one.
        responses.get(
            f"{MONITOR_URL}/cluster/cluster-a/jobs/12",
            status=404,
            json={"detail": "Job 12 not found"},
        )
        result = GetHpcJobTool().execute(self.user, {"job_id": "12"})
        self.assertEqual(result, {"type": "error", "summary": "Job 12 is not found."})

    def test_job_id_must_be_ascii_digits(self):
        result = GetHpcJobTool().execute(self.user, {"job_id": "\u00b2"})
        self.assertEqual(result["type"], "validation_error")
