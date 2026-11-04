"""A job's performance over its run, from the site's slurm-monitor.

Slurm's accounting keeps one summary per job; the time series behind a
per-job report (GPU use, GPU memory and power per card, CPU cores and host
memory over the job's nodes, its processes) come from node probes. Here
that is Sonar on the nodes publishing to Kafka, and slurm-monitor
(github.com/2maz/slurm-monitor) storing the samples and serving them over
REST.

Like FirecREST, slurm-monitor is called with the user's own identity
provider token: it validates the token and only returns the jobs of the
user it names (unless the identity provider gives them its admin role), so
the cluster's view of who may see what applies here too. Waldur adds the
allocation boundary: the job must belong to the workspace's account, which
the caller checks through FirecREST before asking for samples.
"""

import datetime
import logging
import math
import statistics
import time
from collections import defaultdict
from urllib.parse import quote, urlparse

import requests

from waldur_core.configuration import config
from waldur_mastermind.chat.chart_spec import MAX_SERIES, chart, series

from . import allowlist, insights

logger = logging.getLogger(__name__)

API_URL_OPTION = "slurm_monitor_api_url"
CLUSTER_OPTION = "slurm_monitor_cluster_name"

DEFAULT_TIMEOUT = 30
# slurm-monitor refuses longer queries ("job length" limit).
MAX_WINDOW_SECONDS = 14 * 24 * 3600
# Points per series: enough to see a wobble, small enough for a page.
TARGET_POINTS = 200
MIN_RESOLUTION_SECONDS = 60
# "Idle" as the Olivia report counts it: every GPU of the job under 5%.
IDLE_THRESHOLD = 5
TOP_PROCESSES = 10
KIB_PER_GIB = 1024 * 1024


class MonitorException(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def allowed_api_urls() -> set[str]:
    return set(allowlist.parse(config.SLURM_MONITOR_ALLOWED_API_URLS))


def token_audience(api_url) -> allowlist.TokenAudience | None:
    return allowlist.parse(config.SLURM_MONITOR_ALLOWED_API_URLS).get(
        api_url.rstrip("/")
    )


def is_monitoring_enabled(offering) -> bool:
    """The offering names a slurm-monitor endpoint that staff have allowed.

    The user's token is sent there, so the URL must be on the allowlist for
    the same reason as FirecREST's.
    """
    options = offering.plugin_options or {}
    api_url = (options.get(API_URL_OPTION) or "").rstrip("/")
    return bool(
        api_url and options.get(CLUSTER_OPTION) and api_url in allowed_api_urls()
    )


class MonitorClient:
    """slurm-monitor's REST API (``/api/v2``), as one user."""

    def __init__(
        self, api_url, cluster, access_token, timeout=DEFAULT_TIMEOUT, on_behalf_of=""
    ):
        self.api_url = api_url.rstrip("/")
        self.cluster = cluster
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {access_token}"}
        # Who the token belongs to, for the audit log line of each call.
        self.on_behalf_of = on_behalf_of

    def _get(self, path, **params):
        logger.info(
            "slurm-monitor GET %s/cluster/%s%s as %s",
            urlparse(self.api_url).netloc,
            self.cluster,
            path,
            self.on_behalf_of or "unknown user",
        )
        try:
            response = requests.get(
                # The cluster name is the provider's: it stays one segment.
                f"{self.api_url}/cluster/{quote(self.cluster, safe='')}{path}",
                headers=self.headers,
                params={k: v for k, v in params.items() if v is not None},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise MonitorException(f"Unable to reach the cluster monitor: {e}")
        if not response.ok:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.reason
            raise MonitorException(
                f"The cluster monitor returned {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        return response.json()

    def job_process_timeseries(self, job_id, window):
        return self._get(f"/jobs/{job_id}/process/timeseries", **window)

    def job_process_tree(self, job_id, window):
        return self._get(f"/jobs/{job_id}/process/tree", **window)

    def job_report(self, job_id):
        return self._get(f"/jobs/{job_id}/report")

    def node_gpu_timeseries(self, node, window):
        return self._get(f"/nodes/{node}/gpu/timeseries", **window)

    def list_jobs(self, start_time_in_s, end_time_in_s):
        """The caller's jobs sampled in the window, one row per job step.

        slurm-monitor lists a job once its processes have been sampled, so a
        job still pending is not in it.
        """
        return self._get(
            "/jobs",
            start_time_in_s=int(start_time_in_s),
            end_time_in_s=int(end_time_in_s),
        )["jobs"]

    def get_job(self, job_id):
        """The caller's job, without its accounting (``sacct``)."""
        return self._get(f"/jobs/{job_id}")


# slurm-monitor's job records (Sonar's Slurm job samples) in the shape of a
# FirecREST job, so the job list, details, insights and performance read both
# the same way. Units: times are ISO strings, time_limit is in minutes (Sonar
# passes on Slurm's TimelimitRaw; slurm-monitor's API description says
# seconds), requested_memory_per_node in KiB, sacct CPU times in seconds,
# MaxRSS in bytes.


def _epoch(value) -> int | None:
    if not value:
        return None
    if isinstance(value, int | float):
        return int(value)
    return int(datetime.datetime.fromisoformat(value).timestamp())


def _tres(value) -> dict[str, str]:
    """``cpu=8,gres/gpu=2,mem=16G,node=1`` -> ``{"cpu": "8", ...}``."""
    pairs = (item.partition("=") for item in (value or "").split(",") if item)
    return {key: val for key, _, val in pairs}


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def job_from_rows(rows: list[dict]) -> dict:
    """One job from its rows (the job itself and its steps)."""
    main = next((row for row in rows if not row.get("job_step")), rows[0])
    accounting = main.get("sacct") or {}
    tres = _tres(accounting.get("AllocTRES") or main.get("allocated_resources"))
    nodes = main.get("nodes") or []
    node_count = (
        _int(tres.get("node"))
        or main.get("requested_node_count")
        or (len(nodes) or None)
    )
    cpu_times = [accounting.get("UserCPU"), accounting.get("SystemCPU")]
    peaks = [
        (row.get("sacct") or {}).get("MaxRSS")
        for row in rows
        if (row.get("sacct") or {}).get("MaxRSS")
    ]
    memory_per_node = main.get("requested_memory_per_node")
    time_limit = main.get("time_limit")
    return {
        # Array tasks too: slurm-monitor addresses every job by its own id.
        "jobId": str(main["job_id"]),
        "name": main.get("job_name") or "",
        "status": {
            "state": main.get("job_state") or "",
            "stateReason": None,
            "exitCode": main.get("exit_code"),
        },
        "account": main.get("account"),
        "partition": main.get("partition"),
        "user": main.get("user_name"),
        "nodes": ",".join(nodes),
        "allocationNodes": node_count,
        "workingDirectory": None,
        "time": {
            "submission": _epoch(main.get("submit_time")),
            "start": _epoch(main.get("start_time")),
            "end": _epoch(main.get("end_time")),
            "elapsed": accounting.get("ElapsedRaw"),
            "limit": time_limit * 60 if time_limit is not None else None,
            "totalCpu": sum(cpu_times)
            if all(value is not None for value in cpu_times)
            else None,
        },
        "allocationCpus": _int(tres.get("cpu")) or main.get("requested_cpus"),
        "requestedMemory": round(memory_per_node * (node_count or 1) / 1024)
        if memory_per_node
        else None,
        "maxRss": max(peaks) if peaks else None,
        "allocationGpus": _int(tres.get("gres/gpu"))
        or len(main.get("used_gpu_uuids") or [])
        or None,
        "consumedEnergy": None,
    }


def jobs_from_rows(rows: list[dict]) -> list[dict]:
    """Jobs from slurm-monitor's rows, which list each job step separately."""
    by_job = defaultdict(list)
    for row in rows:
        by_job[row.get("job_id")].append(row)
    return [job_from_rows(job_rows) for job_rows in by_job.values()]


def job_window(job: dict, now: float | None = None) -> dict | None:
    """The query window over a job's run (its last 14 days), or None if not started."""
    times = job.get("time") or {}
    start, end = times.get("start"), times.get("end")
    now = now or time.time()
    if not start or start > now:
        return None
    if not end or end < start or insights.outcome(job) is None:
        end = now
    start = max(start, end - MAX_WINDOW_SECONDS + MIN_RESOLUTION_SECONDS)
    resolution = max(MIN_RESOLUTION_SECONDS, math.ceil((end - start) / TARGET_POINTS))
    return {
        "start_time_in_s": int(start),
        "end_time_in_s": int(end),
        "resolution_in_s": resolution,
    }


def _ms(value: str) -> int:
    return int(datetime.datetime.fromisoformat(value).timestamp() * 1000)


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def _round(value, digits=1):
    return None if value is None else round(value, digits)


def _per_card_charts(cards: list[dict]) -> list[dict]:
    """GPU use, memory and power: a grey line per GPU and the mean over GPUs.

    A chart holds MAX_SERIES series; past that the GPUs are summarised by
    their lowest and highest reading at each time.
    """
    specs = []
    metrics = (
        ("GPU use", "%", "ce_util", 1, 100),
        ("GPU memory used", "GiB", "memory", KIB_PER_GIB, None),
        ("GPU power", "W", "power", 1, None),
    )
    for title, unit, field, divisor, y_max in metrics:
        by_time = defaultdict(list)
        lines = []
        for card in cards:
            points = []
            for sample in card["data"]:
                value = sample.get(field)
                if value is None:
                    continue
                value = round(value / divisor, 2)
                moment = _ms(sample["time"])
                by_time[moment].append(value)
                points.append([moment, value])
            lines.append(series(card["label"], points, tone="neutral"))
        moments = sorted(by_time)
        mean = series(
            "Mean over GPUs",
            [[t, round(statistics.fmean(by_time[t]), 2)] for t in moments],
            tone="info",
        )
        if len(lines) + 1 > MAX_SERIES:
            lines = [
                series(
                    "Lowest GPU",
                    [[t, min(by_time[t])] for t in moments],
                    tone="neutral",
                    style="dashed",
                ),
                series(
                    "Highest GPU",
                    [[t, max(by_time[t])] for t in moments],
                    tone="neutral",
                    style="dashed",
                ),
            ]
        specs.append(
            chart(
                "line",
                title,
                x={"type": "time"},
                y={
                    "type": "value",
                    "label": unit,
                    "unit": unit,
                    "min": 0,
                    "max": y_max,
                },
                series=[*lines, mean],
                subtitle=f"{len(cards)} GPUs: each grey line is one GPU, the coloured line is the mean.",
                empty_message="No GPU samples in this window.",
            )
        )
    return specs


def _node_sum_charts(nodes: dict) -> list[dict]:
    """CPU cores used and host memory, summed over the job's nodes."""
    cores = defaultdict(float)
    memory = defaultdict(float)
    for data in nodes.values():
        for sample in data.get("cpu_memory") or []:
            moment = _ms(sample["time"])
            cores[moment] += (sample.get("cpu_util") or 0) / 100
            memory[moment] += (sample.get("memory_resident") or 0) / KIB_PER_GIB
    moments = sorted(cores)
    subtitle = f"Summed over the job's {len(nodes)} node(s)."
    return (
        [
            chart(
                "line",
                "CPU cores used",
                x={"type": "time"},
                y={"type": "value", "label": "cores", "min": 0},
                series=[
                    series(
                        "All nodes",
                        [[t, round(cores[t], 2)] for t in moments],
                        tone="info",
                    )
                ],
                subtitle=subtitle,
                empty_message="No CPU samples in this window.",
            ),
            chart(
                "line",
                "Host memory",
                x={"type": "time"},
                y={"type": "value", "label": "GiB", "unit": "GiB", "min": 0},
                series=[
                    series(
                        "All nodes",
                        [[t, round(memory[t], 2)] for t in moments],
                        tone="info",
                    )
                ],
                subtitle=subtitle,
                empty_message="No memory samples in this window.",
            ),
        ],
        cores,
        memory,
    )


def _processes(tree: dict) -> list[dict]:
    """The job's processes grouped by command, busiest first."""
    groups = defaultdict(lambda: {"nodes": set(), "samples": 0, "cpu": [], "rss": 0})
    for node, data in ((tree or {}).get("nodes") or {}).items():
        for process in (data.get("processes") or {}).values():
            group = groups[process.get("cmd") or "?"]
            group["nodes"].add(node)
            for sample in process.get("data") or []:
                group["samples"] += 1
                group["cpu"].append(sample.get("cpu_util") or 0)
                group["rss"] = max(group["rss"], sample.get("memory_resident") or 0)
    rows = [
        {
            "command": command,
            "nodes": len(group["nodes"]),
            "samples": group["samples"],
            "mean_cpu_percent": _round(_mean(group["cpu"])),
            "max_cpu_percent": _round(max(group["cpu"], default=None)),
            "peak_rss_gib": round(group["rss"] / KIB_PER_GIB, 2),
        }
        for command, group in groups.items()
    ]
    rows.sort(key=lambda row: row["mean_cpu_percent"] or 0, reverse=True)
    return rows[:TOP_PROCESSES]


def _job_nodes(job: dict, samples: dict) -> list[str]:
    nodes = set(samples)
    if job.get("nodes") and job["nodes"] != "None assigned":
        nodes |= insights.expand_hostlist(job["nodes"])
    return sorted(nodes)


def unavailable(message: str) -> dict:
    return {"available": False, "message": message}


def job_performance(client: MonitorClient, job: dict, now: float | None = None) -> dict:
    """Summary tiles, charts and processes for a job the caller may see."""
    window = job_window(job, now)
    if window is None:
        return unavailable("The cluster samples a job once it is running.")
    job_id = job["jobId"]

    timeseries = client.job_process_timeseries(job_id, window)
    samples = (timeseries[0].get("nodes") if timeseries else None) or {}
    if not samples:
        return unavailable(
            "The cluster monitor has no samples for this job in its run time."
        )

    used_gpus = {uuid for data in samples.values() for uuid in data.get("gpus") or {}}
    cards = []
    if used_gpus:
        for node in _job_nodes(job, samples):
            for card in (client.node_gpu_timeseries(node, window) or {}).get(node, []):
                if card["uuid"] in used_gpus:
                    cards.append({**card, "label": f"{node} · GPU {card['index']}"})
        cards.sort(key=lambda card: card["label"])

    node_charts, cores, memory = _node_sum_charts(samples)
    charts = [*_per_card_charts(cards), *node_charts] if cards else node_charts

    card_samples = [sample for card in cards for sample in card["data"]]
    by_time = defaultdict(list)
    for sample in card_samples:
        by_time[sample["time"]].append(sample.get("ce_util") or 0)
    memory_totals = [
        sample["memory"] / (sample["memory_util"] / 100) / KIB_PER_GIB
        for sample in card_samples
        if sample.get("memory") and sample.get("memory_util")
    ]
    moments = sorted(set(cores) | {_ms(t) for t in by_time})
    # Coverage: the time the samples account for, at the probe's own interval
    # (the usual gap between samples), out of the job's run in the window.
    gaps = [(b - a) / 1000 for a, b in zip(moments, moments[1:])]
    interval = statistics.median(gaps) if gaps else window["resolution_in_s"]
    span = max(window["end_time_in_s"] - window["start_time_in_s"], interval)
    summary = {
        "nodes": len(samples),
        "gpus": len(cards) or None,
        "gpu_hours_observed": _round(len(card_samples) * interval / 3600)
        if cards
        else None,
        "gpu_use_percent": _round(_mean(s.get("ce_util") for s in card_samples)),
        "idle_share_percent": _round(
            100
            * sum(1 for v in by_time.values() if max(v) < IDLE_THRESHOLD)
            / len(by_time)
        )
        if by_time
        else None,
        "gpu_memory_percent": _round(_mean(s.get("memory_util") for s in card_samples)),
        "gpu_memory_peak_gib": _round(
            max((s.get("memory") or 0 for s in card_samples), default=0) / KIB_PER_GIB
        )
        if cards
        else None,
        "gpu_memory_total_gib": _round(max(memory_totals)) if memory_totals else None,
        "gpu_power_watts": _round(_mean(s.get("power") for s in card_samples), 0),
        "cpu_cores_mean": _round(_mean(cores.values())),
        "host_memory_peak_gib": _round(max(memory.values(), default=None)),
        "host_memory_mean_gib": _round(_mean(memory.values())),
        "samples": len(moments),
        "coverage_percent": _round(min(100.0, 100 * len(moments) * interval / span)),
        "first_sample": moments[0] // 1000 if moments else None,
        "last_sample": moments[-1] // 1000 if moments else None,
    }

    try:
        processes = _processes(client.job_process_tree(job_id, window))
    except MonitorException:
        processes = []  # the tree is a detail; the report stands without it
    try:
        warnings = client.job_report(job_id).get("warnings") or []
    except MonitorException:
        warnings = []

    return {
        "available": True,
        "message": "",
        "start_time": window["start_time_in_s"],
        "end_time": window["end_time_in_s"],
        "resolution": window["resolution_in_s"],
        "summary": summary,
        "charts": charts,
        "processes": processes,
        "warnings": warnings,
    }
