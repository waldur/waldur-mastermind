"""HPC tools that read the cluster: allocations, partitions, software."""

from dataclasses import asdict

from waldur_firecrest.workspace import resources_for_user
from waldur_mastermind.chat.tools.base import MAX_LIST_RESULTS, BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.hpc.helpers import (
    ALLOCATION_PROPERTY,
    allocation_summary,
    run,
)
from waldur_mastermind.chat.tools.registry import tool_registry


class ListHpcAllocationsTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_HPC_ALLOCATIONS,
            category=ToolCategory.HPC,
            description=(
                "List the user's HPC (SLURM) allocations: allocation uuid, Slurm "
                "account, cluster, project, and can_run_jobs (false when only a "
                "cluster monitor is connected: jobs can then be looked at, not "
                "submitted or canceled)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
            usage_instructions=(
                "Call first for any HPC request (jobs, partitions, software, files) "
                "when you do not yet know which allocation the user means."
            ),
            workflow_instructions=(
                "HPC job workflow: list_hpc_allocations -> get_hpc_cluster_status "
                "(partitions and their limits) -> find_hpc_software (module lines) "
                "-> plan_hpc_job (preview card) -> user confirms -> submit_hpc_job "
                "-> list_hpc_jobs / get_hpc_job to follow it. When a job failed, "
                "read its output with get_hpc_job and explain the cause. For "
                "questions about allocation use, job history or cluster load use "
                "get_hpc_allocation_usage, get_hpc_job_stats and "
                "get_hpc_cluster_load; plot_hpc_output charts a result file."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        resources = resources_for_user(user)
        total = resources.count()
        allocations = [allocation_summary(r) for r in resources[:MAX_LIST_RESULTS]]
        return {
            "type": "success",
            "summary": f"{total} HPC allocation(s).",
            "data": {
                "allocations": allocations,
                "_total_count": total,
                "_truncated": total > len(allocations),
            },
        }


class GetHpcClusterStatusTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_HPC_CLUSTER_STATUS,
            category=ToolCategory.HPC,
            description=(
                "Cluster status for an allocation: service health and partitions with "
                "idle/total nodes, time/node/CPU/memory limits, CPU/GPU architecture "
                "and allowed QoS. Health and node counts come from FirecREST: with "
                "only a cluster monitor (can_run_jobs false) the partitions are the "
                "ones Waldur publishes, with their limits."
            ),
            inputSchema={
                "type": "object",
                "properties": {**ALLOCATION_PROPERTY},
                "required": [],
            },
            usage_instructions=(
                "Use before planning a job to pick a partition within its limits, and "
                "for questions like 'is the cluster busy?' or 'which partition has GPUs?'."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        def action(workspace):
            allocation = allocation_summary(workspace.resource)
            data = {"allocation": allocation}
            if workspace.has_firecrest:
                system = workspace.system_status()
                data["health"] = [
                    {"service": item.get("serviceType"), "healthy": item.get("healthy")}
                    for item in system.get("servicesHealth") or []
                ]
            data["partitions"] = workspace.partitions()
            summary = (
                f"{len(data['partitions'])} partition(s) on {allocation['cluster']}."
            )
            if not workspace.has_firecrest:
                summary += " Service health and free nodes need FirecREST."
            return {"type": "success", "summary": summary, "data": data}

        return run(user, arguments, action)


class FindHpcSoftwareTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.FIND_HPC_SOFTWARE,
            category=ToolCategory.HPC,
            description=(
                "Search the software catalogs (EESSI, Spack, ...) available on the "
                "allocation's cluster. Returns versions with the exact setup and "
                "'module load'/'spack load' lines to put in a job."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **ALLOCATION_PROPERTY,
                    "query": {
                        "type": "string",
                        "description": "Software name or keyword, e.g. 'gromacs'.",
                    },
                    "partition": {
                        "type": "string",
                        "description": "Only builds for this partition's CPU architecture.",
                    },
                },
                "required": ["query"],
            },
            usage_instructions=(
                "Use whenever a job needs an application or library. Never invent "
                "module names: copy 'setup' and 'load' lines from this tool into "
                "plan_hpc_job's `modules`. Pass `partition` once one is chosen."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        def action(workspace):
            matches = workspace.find_software(
                (arguments.get("query") or "").strip(),
                (arguments.get("partition") or "").strip(),
                limit=MAX_LIST_RESULTS,
            )
            if not matches:
                return {
                    "type": "success",
                    "summary": "No matching software in the cluster's catalogs.",
                    "data": {"software": []},
                }
            return {
                "type": "success",
                "summary": f"{len(matches)} matching software version(s).",
                "data": {"software": [asdict(m) for m in matches]},
            }

        return run(user, arguments, action)


tool_registry.register(ListHpcAllocationsTool())
tool_registry.register(GetHpcClusterStatusTool())
tool_registry.register(FindHpcSoftwareTool())
