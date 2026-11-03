"""A job's performance report as a PDF, compiled with Typst like invoices.

Python does all the formatting and scales chart points to 0..1, so the
template (job_report.typ) only lays out text, tiles, lines and tables. The
charts are drawn as Typst curves from the same chart specs the page
renders, so the PDF shows what the user saw.
"""

import datetime
import json
import math
import threading
from pathlib import Path

import typst
from django.utils import timezone

import waldur_mastermind.invoices

PDF_DIR = Path(__file__).parent
# The invoices' bundled Source Sans 3 (OFL); one copy in the image.
FONT_DIR = Path(waldur_mastermind.invoices.__file__).parent / "pdf" / "fonts"

GREY = "#c3c8cf"
TEAL = "#0f7c8c"
TONE_COLORS = {
    "info": TEAL,
    "success": "#15803d",
    "warning": "#d97706",
    "danger": "#b91c1c",
}
IDLE_FLAG_PERCENT = 20
EMPTY = "—"

_compiler = None
_compiler_lock = threading.Lock()


def _local(seconds) -> datetime.datetime:
    return timezone.localtime(datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC))


def _when(seconds) -> str:
    return _local(seconds).strftime("%Y-%m-%d %H:%M") if seconds else EMPTY


def _duration(seconds) -> str:
    if seconds is None:
        return EMPTY
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    clock = f"{hours:02d}:{minutes:02d}"
    return f"{days} d {clock}" if days else clock


def _number(value, digits=1, unit="") -> str:
    if value is None:
        return EMPTY
    text = f"{value:,.{digits}f}"
    return f"{text} {unit}".rstrip()


def _nice_step(span: float, ticks: int = 5) -> float:
    raw = span / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _y_axis(spec: dict, values: list[float]) -> tuple[float, list[dict]]:
    top = spec["y"].get("max")
    if not top:
        top = max(values, default=0) * 1.08 or 1
        step = _nice_step(top)
        top = math.ceil(top / step) * step
    step = _nice_step(top)
    ticks = []
    value = 0.0
    while value <= top + step / 1000:
        label = f"{value:g}" if step >= 1 else f"{value:.1f}"
        ticks.append({"pos": value / top, "label": label})
        value += step
    return top, ticks


# Tick spacings (seconds): the first that leaves at most MAX_X_TICKS ticks.
X_TICK_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
MAX_X_TICKS = 7


def _x_ticks(start: float, end: float) -> list[dict]:
    """Ticks on round local times (every 5 min, 1 h, …) across start..end."""
    span = max(end - start, 1)
    step = next((s for s in X_TICK_STEPS if span / s <= MAX_X_TICKS), X_TICK_STEPS[-1])
    fmt = "%d %b %H:%M" if span > 86400 else "%H:%M"
    # Round in local time, so hourly ticks land on the hour where the reader is.
    offset = _local(start).utcoffset().total_seconds()
    first = math.ceil((start + offset) / step) * step - offset
    ticks, moment = [], first
    while moment <= end:
        ticks.append(
            {
                "pos": round((moment - start) / span, 4),
                "label": _local(moment).strftime(fmt),
            }
        )
        moment += step
    return ticks


def _segments(points, start_ms, span_ms, top) -> list[list[list[float]]]:
    """Points scaled to 0..1, split where a value is missing."""
    segments, current = [], []
    for x, y in points:
        if y is None:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(
            [
                round(min(max((x - start_ms) / span_ms, 0), 1), 4),
                round(min(max(y / top, 0), 1), 4),
            ]
        )
    if current:
        segments.append(current)
    return segments


def chart_geometry(spec: dict, start: int, end: int) -> dict:
    values = [
        y for s in spec["series"] for _, y in s["points"] if isinstance(y, (int, float))
    ]
    top, y_ticks = _y_axis(spec, values)
    # The axis covers the job's window and every sample: the monitor stamps a
    # sample with the start of its bucket, which can precede the job's start.
    moments = [x for s in spec["series"] for x, _ in s["points"]]
    if moments:
        start = min(start, min(moments) / 1000)
        end = max(end, max(moments) / 1000)
    start_ms, span_ms = start * 1000, max(end - start, 1) * 1000
    lines, legend, grey_count = [], [], 0
    for item in spec["series"]:
        grey = item.get("tone", "neutral") == "neutral"
        grey_count += grey
        color = GREY if grey else TONE_COLORS.get(item.get("tone"), TEAL)
        lines.append(
            {
                "color": color,
                "width": 0.5 if grey else 1.4,
                "dashed": item.get("style") == "dashed",
                "segments": _segments(item["points"], start_ms, span_ms, top),
            }
        )
        if not grey:
            legend.append({"label": item["name"], "color": color})
    if grey_count > 2:
        legend.insert(0, {"label": "Each GPU", "color": GREY})
    elif grey_count:
        legend[:0] = [
            {"label": s["name"], "color": GREY}
            for s in spec["series"]
            if s.get("tone", "neutral") == "neutral"
        ]
    return {
        "title": spec["title"],
        "subtitle": spec.get("subtitle", ""),
        "unit": spec["y"].get("unit") or spec["y"].get("label") or "",
        "y_ticks": y_ticks,
        "x_ticks": _x_ticks(start, end),
        "lines": lines,
        "legend": legend,
        "empty": "" if values else spec.get("empty_message", "No samples."),
    }


def _tiles(job: dict, summary: dict) -> list[dict]:
    def tile(label, value, detail="", flag=False):
        return {"label": label, "value": value, "detail": detail, "flag": flag}

    tiles = []
    if summary.get("gpus"):
        idle = summary.get("idle_share_percent")
        tiles += [
            tile("GPUs", str(summary["gpus"]), f"{summary['nodes']} node(s)"),
            tile(
                "GPU-hours observed",
                _number(summary.get("gpu_hours_observed")),
                f"{summary['samples']} samples",
            ),
            tile(
                "Real GPU-hours",
                _number(job.get("gpu_hours")),
                f"coverage {_number(summary.get('coverage_percent'), 0)}%, "
                f"{job.get('allocation_gpus') or summary['gpus']} GPUs x "
                f"{_duration(job.get('elapsed'))}",
            ),
            tile("GPU use", _number(summary.get("gpu_use_percent"), 0, "%"), "mean"),
            tile(
                "Idle share",
                _number(idle, 0, "%"),
                "samples with max use under 5%",
                flag=bool(idle and idle >= IDLE_FLAG_PERCENT),
            ),
            tile(
                "GPU memory",
                _number(summary.get("gpu_memory_percent"), 0, "%"),
                f"peak {_number(summary.get('gpu_memory_peak_gib'))} of "
                f"{_number(summary.get('gpu_memory_total_gib'), 0)} GiB",
            ),
            tile(
                "Power", _number(summary.get("gpu_power_watts"), 0, "W"), "mean per GPU"
            ),
        ]
    else:
        tiles += [
            tile("Nodes", str(summary["nodes"])),
            tile(
                "Coverage",
                _number(summary.get("coverage_percent"), 0, "%"),
                f"{summary['samples']} samples",
            ),
            tile(
                "CPU efficiency",
                _number((job.get("cpu_efficiency") or 0) * 100, 0, "%")
                if job.get("cpu_efficiency") is not None
                else EMPTY,
                "from accounting",
            ),
        ]
    tiles += [
        tile("CPU cores", _number(summary.get("cpu_cores_mean")), "mean, all nodes"),
        tile(
            "Host memory",
            _number(summary.get("host_memory_peak_gib"), 1, "GiB"),
            f"peak; mean {_number(summary.get('host_memory_mean_gib'), 1, 'GiB')}",
        ),
    ]
    return tiles


def _accounting_tiles(job: dict) -> list[dict]:
    """Figures from Slurm's accounting: all a report has without a monitor."""

    def tile(label, value, detail=""):
        return {"label": label, "value": value, "detail": detail, "flag": False}

    def percent(ratio):
        return EMPTY if ratio is None else _number(ratio * 100, 0, "%")

    tiles = [
        tile(
            "CPUs",
            str(job.get("allocation_cpus") or EMPTY),
            f"{job.get('allocation_nodes') or 1} node(s)",
        ),
        tile(
            "CPU efficiency",
            percent(job.get("cpu_efficiency")),
            f"{_number(job.get('cpu_hours'))} core-hours",
        ),
        tile(
            "Peak memory",
            _number((job.get("max_rss_mb") or 0) / 1024, 1, "GiB")
            if job.get("max_rss_mb")
            else EMPTY,
            f"efficiency {percent(job.get('memory_efficiency'))}",
        ),
    ]
    if job.get("allocation_gpus"):
        tiles.append(
            tile(
                "GPUs",
                str(job["allocation_gpus"]),
                f"{_number(job.get('gpu_hours'))} GPU-hours",
            )
        )
    if job.get("energy_kwh"):
        tiles.append(
            tile(
                "Energy",
                _number(job["energy_kwh"], 1, "kWh"),
                f"{_number(job.get('mean_power_w'), 0, 'W')} mean",
            )
        )
    return tiles


def _facts(job: dict, performance: dict) -> list[dict]:
    summary = performance.get("summary") or {}
    state = job.get("state") or EMPTY
    if job.get("exit_code") is not None:
        state = f"{state} (exit code {job['exit_code']})"
    rows = [
        ("Job ID", job.get("job_id")),
        ("Name", job.get("name")),
        ("User", job.get("user")),
        ("Account", job.get("account")),
        ("Partition", job.get("partition")),
        ("Nodes", job.get("nodes")),
        ("State", state),
        ("Submitted", _when(job.get("submit_time"))),
        ("Start", _when(job.get("start_time"))),
        ("End", _when(job.get("end_time")) if job.get("end_time") else "still running"),
        ("Elapsed", _duration(job.get("elapsed"))),
        ("Time limit", _duration(job.get("time_limit"))),
        ("Energy", _number(job.get("energy_kwh"), 1, "kWh")),
    ]
    if summary:
        rows += [
            ("Samples seen", str(summary["samples"])),
            ("First sample", _when(summary.get("first_sample"))),
            ("Last sample", _when(summary.get("last_sample"))),
        ]
    return [{"label": label, "value": str(value or EMPTY)} for label, value in rows]


def _processes(performance: dict) -> dict:
    rows = [
        [
            row["command"],
            str(row["nodes"]),
            str(row["samples"]),
            _number(row.get("mean_cpu_percent")),
            _number(row.get("max_cpu_percent")),
            _number(row.get("peak_rss_gib"), 2),
        ]
        for row in performance.get("processes") or []
    ]
    return {
        "headers": [
            "Command",
            "Nodes",
            "Samples",
            "Mean CPU %",
            "Max CPU %",
            "RSS peak GiB",
        ],
        "rows": rows,
        "note": "Grouped by command; the probe records command names, not full "
        "command lines."
        if rows
        else "",
    }


def report_data(job: dict, performance: dict, workspace_name: str) -> dict:
    """The template input: a serialized HpcJob and a job_performance result."""
    available = performance.get("available")
    charts = (
        [
            chart_geometry(spec, performance["start_time"], performance["end_time"])
            for spec in performance.get("charts") or []
        ]
        if available
        else []
    )
    title = f"Job {job.get('job_id')}"
    if job.get("name"):
        title += f" · {job['name']}"
    return {
        "title": title,
        "subtitle": workspace_name,
        "generated": f"Generated {timezone.localtime().strftime('%Y-%m-%d %H:%M %Z')}",
        "footer": f"{title} · {workspace_name}",
        "notice": "" if available else performance.get("message", ""),
        "tiles": _tiles(job, performance["summary"])
        if available
        else _accounting_tiles(job),
        "warnings": performance.get("warnings") or [],
        "charts": charts,
        "facts": _facts(job, performance),
        "processes": _processes(performance),
        "labels": {"job": "Job", "processes": "Processes"},
    }


def job_report_pdf(job: dict, performance: dict, workspace_name: str) -> bytes:
    """Compile the report; the compiler (font scan) is built once per process."""
    global _compiler
    data = json.dumps(report_data(job, performance, workspace_name))
    with _compiler_lock:
        if _compiler is None:
            _compiler = typst.Compiler(
                str(PDF_DIR / "job_report.typ"),
                root=str(PDF_DIR),
                font_paths=[str(FONT_DIR)],
            )
        return _compiler.compile(sys_inputs={"report": data})
