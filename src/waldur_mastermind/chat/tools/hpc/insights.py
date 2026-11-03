"""HPC tools that answer with figures: allocation usage, job history, cluster load.

Each tool returns compact numbers for the model to reason about and a chart
for the user. Charts are built here from the same data, never written by the
model, so the picture always matches the figures in the answer.
"""

import datetime
import statistics
from collections import defaultdict

from django.utils import timezone

from waldur_firecrest import charts
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.hpc.helpers import (
    ALLOCATION_PROPERTY,
    allocation_summary,
    run,
)
from waldur_mastermind.chat.tools.registry import tool_registry

JOB_VIEWS = {
    "outcomes": charts.outcomes,
    "timeline": charts.job_timeline,
    "time_limits": charts.walltime_fit,
    "efficiency": charts.efficiency,
    "queue_wait": charts.queue_wait,
}


def _local_time(epoch: int) -> str:
    """'Tue 20 Oct 2026 09:18 EEST': models misjudge weekdays from timestamps."""
    moment = timezone.localtime(datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC))
    return moment.strftime("%a %d %b %Y %H:%M %Z")


def _time_limit_fit(walltime: list[dict]) -> dict:
    """How much of their limits finished jobs used, overall and per job name."""
    finished = [p for p in walltime if p["outcome"] == "completed"]
    by_name = defaultdict(list)
    for point in finished:
        by_name[point["name"]].append(point)
    return {
        "completed_jobs": len(finished),
        "median_used_percent": round(
            statistics.median(p["ratio"] for p in finished) * 100
        )
        if finished
        else None,
        "by_name": [
            {
                "name": name,
                "jobs": len(points),
                "median_used_percent": round(
                    statistics.median(p["ratio"] for p in points) * 100
                ),
                "median_requested_minutes": statistics.median(
                    p["requested_minutes"] for p in points
                ),
                "longest_used_minutes": max(p["used_minutes"] for p in points),
            }
            for name, points in sorted(by_name.items(), key=lambda item: -len(item[1]))[
                :8
            ]
        ],
    }


def _chart_result(summary: str, data: dict, chart: dict) -> dict:
    return {
        "type": "success",
        "summary": summary,
        "data": data,
        "ui_component": "chart",
        "ui_data": chart,
    }


class GetHpcAllocationUsageTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_HPC_ALLOCATION_USAGE,
            category=ToolCategory.HPC,
            description=(
                "How much of an HPC allocation is used: consumption of each limited "
                "component (CPU-hours, GPU-hours, ...) against its limit, the recent "
                "daily pace, the date it runs out at that pace and use per user. "
                "Shows the user a burn-down chart."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **ALLOCATION_PROPERTY,
                    "component": {
                        "type": "string",
                        "description": "Component type to chart, e.g. 'cpu' or 'gpu'. "
                        "Defaults to the first limited one.",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["burn_down", "by_user"],
                        "description": "Chart against the limit (default) or per user.",
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for 'how much allocation is left?', 'will it last?', 'who is "
                "using it?'. Quote the numbers from the result; when it runs out "
                "before the period ends, say when and suggest get_hpc_job_stats "
                "to find what consumes it."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        def action(workspace):
            usage = workspace.allocation_usage()
            components = usage["components"]
            if not components:
                return {
                    "type": "success",
                    "summary": "The allocation has no limited components or reported usage.",
                    "data": {"allocation": allocation_summary(workspace.resource)},
                }
            wanted = (arguments.get("component") or "").strip().lower()
            component = next(
                (c for c in components if c["type"].lower() == wanted),
                next((c for c in components if c["limit"]), components[0]),
            )
            link = charts.insights_path(workspace.resource.uuid.hex)
            if arguments.get("view") == "by_user":
                chart = charts.usage_by_user(component)
            else:
                chart = charts.allocation_burn(component, link=link)
            data = {
                "allocation": allocation_summary(workspace.resource),
                "as_of": usage["as_of"],
                "components": [
                    {
                        key: c[key]
                        for key in (
                            "type",
                            "name",
                            "unit",
                            "limit",
                            "limit_period",
                            "period_start",
                            "period_end",
                            "consumed",
                            "percent",
                            "daily_rate",
                            "runs_out",
                            "runs_out_before_period_end",
                        )
                    }
                    | {
                        "top_users": [
                            {"username": u["username"], "total": u["total"]}
                            for u in c["users"][:5]
                        ]
                    }
                    for c in components
                ],
            }
            return _chart_result(
                f"{component['name']}: {chart.get('subtitle', '')}.", data, chart
            )

        return run(user, arguments, action)


class GetHpcJobStatsTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_HPC_JOB_STATS,
            category=ToolCategory.HPC,
            description=(
                "Statistics of an allocation's jobs over recent days: outcomes, "
                "failures by exit code, requested vs used time, CPU and memory "
                "efficiency, queue wait per partition, and right-sizing suggestions. "
                "Shows the user one chart, chosen by `view`."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **ALLOCATION_PROPERTY,
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                        "description": "How many days back (default and most: 7).",
                    },
                    "view": {
                        "type": "string",
                        "enum": list(JOB_VIEWS),
                        "description": "Chart to show: outcomes per day (default), "
                        "timeline of recent jobs, time_limits (requested vs used), "
                        "efficiency (CPU and memory) or queue_wait per partition.",
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for 'how are my jobs doing?', 'why do my jobs fail?', 'am I "
                "wasting allocation?', 'which partition starts fastest?'. Pick the "
                "view that answers the question, explain the figures, and pass on "
                "the `suggestions` in plain words. Offer plan_hpc_job with a "
                "suggested time limit when one applies."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        view = arguments.get("view") or "outcomes"
        if view not in JOB_VIEWS:
            return {
                "type": "validation_error",
                "summary": f"view must be one of {', '.join(JOB_VIEWS)}.",
            }
        try:
            days = int(arguments.get("days") or 7)
        except (TypeError, ValueError):
            return {"type": "validation_error", "summary": "days must be a number."}

        def action(workspace):
            stats = workspace.job_stats(days)
            chart = JOB_VIEWS[view](stats)
            chart["link"] = {
                "label": "Open insights",
                "url": charts.insights_path(workspace.resource.uuid.hex),
            }
            efficiency = [p["cpu_efficiency"] for p in stats["efficiency"]]
            data = {
                "allocation": allocation_summary(workspace.resource),
                "days": stats["days"],
                "totals": stats["totals"],
                "cpu_hours": stats["cpu_hours"],
                "failures": stats["failures"][:5],
                "wait_by_partition": stats["wait_by_partition"],
                "time_limit_fit": _time_limit_fit(stats["walltime"]),
                "median_cpu_efficiency": sorted(efficiency)[len(efficiency) // 2]
                if efficiency
                else None,
                "suggestions": stats["suggestions"],
            }
            totals = stats["totals"]
            finished = sum(
                totals[k] for k in ("completed", "failed", "timeout", "cancelled")
            )
            return _chart_result(
                f"{finished} job(s) finished in the last {stats['days']} days: "
                f"{totals['completed']} completed, {totals['failed']} failed, "
                f"{totals['timeout']} timed out, {totals['cancelled']} cancelled.",
                data,
                chart,
            )

        return run(user, arguments, action)


class GetHpcClusterLoadTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_HPC_CLUSTER_LOAD,
            category=ToolCategory.HPC,
            description=(
                "How busy the allocation's cluster is: nodes per partition that are "
                "idle, partly or fully used, drained or down, average CPU load, and "
                "upcoming reservations and maintenance windows. Shows a node map or "
                "the reservation timeline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **ALLOCATION_PROPERTY,
                    "partition": {
                        "type": "string",
                        "description": "Only this partition's nodes in the map.",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["nodes", "reservations"],
                        "description": "Node map (default) or reservations.",
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use for 'is the cluster busy?', 'where will my job start soonest?', "
                "'is there maintenance coming?', 'why is my job pending?'. Quote the "
                "provider's announcement message for planned maintenance, mention "
                "maintenance a long job would run into, and say when an announced "
                "outage is not held on the cluster (reservation_in_place false: running "
                "jobs may be stopped) or a cluster maintenance was never announced."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        partition = (arguments.get("partition") or "").strip()

        def action(workspace):
            load = workspace.cluster_load()
            known = {p["name"] for p in load["partitions"]}
            if partition and partition not in known:
                return {
                    "type": "validation_error",
                    "summary": f"No partition '{partition}'. Known: {', '.join(sorted(known))}.",
                }
            if arguments.get("view") == "reservations":
                chart = charts.reservations(load)
            else:
                chart = charts.node_map(load, partition)
            data = {
                "allocation": allocation_summary(workspace.resource),
                "partitions": load["partitions"],
                "unavailable_nodes": [
                    {k: n[k] for k in ("name", "state", "reason", "partitions")}
                    for n in load["nodes"]
                    if n["state"] in ("DOWN", "DRAIN", "MAINTENANCE")
                ],
                # The provider's own announcements: what and why, for any
                # service. Reservations below are what the scheduler enforces.
                "announcements": [
                    {
                        **{
                            k: a[k]
                            for k in (
                                "name",
                                "message",
                                "state",
                                "impact",
                                "impact_description",
                                "partitions",
                                "reservation_in_place",
                            )
                        },
                        "starts": _local_time(a["start"]),
                        "ends": _local_time(a["end"]),
                    }
                    for a in load["announcements"]
                ],
                "reservations": [
                    {
                        **{
                            k: r[k]
                            for k in (
                                "name",
                                "maintenance",
                                "active",
                                "partitions",
                                "node_list",
                                "announced",
                            )
                        },
                        "starts": _local_time(r["start"]),
                        "ends": _local_time(r["end"]),
                    }
                    for r in load["reservations"]
                ],
            }
            idle = sum(p["idle"] for p in load["partitions"])
            total = sum(p["total"] for p in load["partitions"])
            return _chart_result(
                f"{idle} of {total} partition node slots idle; "
                f"{len(load['reservations'])} upcoming reservation(s).",
                data,
                chart,
            )

        return run(user, arguments, action)


class PlotHpcOutputTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.PLOT_HPC_OUTPUT,
            category=ToolCategory.HPC,
            description=(
                "Plot a job's output file from the cluster: GROMACS .xvg (energies, "
                "temperature, RMSD...), CSV/TSV tables, or a WRF rsl.error.0000/"
                "rsl.out.0000 log (forecast progress and time left). Returns a "
                "summary of each series and shows the chart."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **ALLOCATION_PROPERTY,
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file on the cluster.",
                    },
                    "forecast_hours": {
                        "type": "number",
                        "description": "WRF only: forecast length when namelist.input "
                        "next to the log does not give it.",
                    },
                },
                "required": ["path"],
            },
            usage_instructions=(
                "Use when the user asks to see or check a result file, whether a "
                "simulation has equilibrated, or how far a running WRF forecast is. "
                "If you do not know the path, list the job's working directory first."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        path = (arguments.get("path") or "").strip()
        if not path.startswith("/"):
            return {
                "type": "validation_error",
                "summary": "path must be an absolute path on the cluster.",
            }
        forecast_hours = arguments.get("forecast_hours")

        def action(workspace):
            data = workspace.output_series(path, forecast_hours)
            chart = charts.output_series(data)
            return _chart_result(
                f"Plotted {data['title']} from {path}.",
                {"path": path, "kind": data["kind"], "summary": data["summary"]},
                chart,
            )

        return run(user, arguments, action)


tool_registry.register(GetHpcAllocationUsageTool())
tool_registry.register(GetHpcJobStatsTool())
tool_registry.register(GetHpcClusterLoadTool())
tool_registry.register(PlotHpcOutputTool())
