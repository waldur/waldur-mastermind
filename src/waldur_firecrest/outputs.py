"""Series read from job output files, by file type.

A small registry of parsers for text outputs that scientific codes write and
users want to see plotted: GROMACS ``.xvg`` (gmx energy and friends), CSV/TSV
tables, and WRF's ``rsl.error.*`` / ``rsl.out.*`` logs, whose "Timing for
main" lines give a run's progress. Each parser turns text into named series
of ``[x, y]`` points plus a short summary for the assistant.
"""

import csv
import datetime
import io
import math
import re
from dataclasses import dataclass, field

MAX_SERIES = 8
MAX_POINTS = 2000


class UnsupportedOutput(Exception):
    pass


@dataclass
class OutputSeries:
    kind: str
    title: str
    x_label: str
    y_label: str = ""
    x_type: str = "value"
    series: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "title": self.title,
            "x_label": self.x_label,
            "y_label": self.y_label,
            "x_type": self.x_type,
            "series": self.series,
            "summary": self.summary,
        }


def kind_for_path(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(".xvg"):
        return "xvg"
    if name.endswith((".csv", ".tsv")):
        return "csv"
    if re.match(r"rsl\.(error|out)\.\d+$", name):
        return "wrf_rsl"
    return None


def _decimate(points: list) -> list:
    if len(points) <= MAX_POINTS:
        return points
    step = math.ceil(len(points) / MAX_POINTS)
    return points[::step] + ([points[-1]] if (len(points) - 1) % step else [])


def _describe(points: list) -> dict:
    values = [y for _, y in points if y is not None]
    if not values:
        return {"points": 0}
    tail = values[len(values) // 2 :]
    return {
        "points": len(points),
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "mean_second_half": round(sum(tail) / len(tail), 4),
    }


def parse_xvg(text: str) -> OutputSeries:
    """GROMACS xvg: '#' comments, '@' xmgrace directives, whitespace columns."""
    title, x_label, y_label, legends = "GROMACS output", "x", "", {}
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            quoted = re.search(r'"(.*)"', line)
            value = quoted.group(1) if quoted else ""
            if re.match(r"@\s+title", line):
                title = value
            elif re.match(r"@\s+xaxis\s+label", line):
                x_label = value
            elif re.match(r"@\s+yaxis\s+label", line):
                y_label = value
            elif match := re.match(r"@\s+s(\d+)\s+legend", line):
                legends[int(match.group(1))] = value
            continue
        try:
            rows.append([float(v) for v in line.split()])
        except ValueError:
            continue
    if not rows:
        raise UnsupportedOutput("No data rows found in the xvg file.")
    columns = min(len(row) for row in rows)
    series = []
    for index in range(1, min(columns, MAX_SERIES + 1)):
        points = _decimate([[row[0], row[index]] for row in rows])
        series.append(
            {"name": legends.get(index - 1, f"column {index + 1}"), "points": points}
        )
    return OutputSeries(
        kind="xvg",
        title=title,
        x_label=x_label,
        y_label=y_label,
        series=series,
        summary={s["name"]: _describe(s["points"]) for s in series},
    )


def _number(value: str):
    try:
        return float(value)
    except ValueError:
        return None


def parse_csv(text: str, path: str = "") -> OutputSeries:
    delimiter = "\t" if path.lower().endswith(".tsv") else None
    sample = text[:4096]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t ").delimiter
        except csv.Error:
            delimiter = ","
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]
    if len(rows) < 2:
        raise UnsupportedOutput("The table has no data rows.")
    header = rows[0]
    has_header = any(_number(cell) is None for cell in header)
    data = rows[1:] if has_header else rows
    names = header if has_header else [f"column {i + 1}" for i in range(len(header))]
    numeric = [
        index
        for index in range(1, len(names))
        if sum(_number(row[index]) is not None for row in data if index < len(row))
        >= len(data) / 2
    ][:MAX_SERIES]
    if not numeric:
        raise UnsupportedOutput("No numeric columns to plot.")
    x_values = [_number(row[0]) if row else None for row in data]
    use_index = any(x is None for x in x_values)
    series = []
    for index in numeric:
        points = [
            [i if use_index else x_values[i], _number(row[index])]
            for i, row in enumerate(data)
            if index < len(row)
        ]
        series.append({"name": names[index], "points": _decimate(points)})
    return OutputSeries(
        kind="csv",
        title=path.rsplit("/", 1)[-1] or "Table",
        x_label="row" if use_index else names[0],
        series=series,
        summary={s["name"]: _describe(s["points"]) for s in series},
    )


WRF_TIMING = re.compile(
    r"Timing for main: time (\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}) "
    r"on domain\s+(\d+):\s+([\d.]+) elapsed seconds"
)
WRF_TIME = "%Y-%m-%d_%H:%M:%S"


def parse_wrf_rsl(text: str, forecast_hours: float | None = None) -> OutputSeries:
    """Simulated forecast hours against wall-clock hours, from domain 1 timings.

    Each "Timing for main" line reports the model time reached and the wall
    seconds that step took; their running sum is the wall time so far.
    """
    steps = []
    for match in WRF_TIMING.finditer(text):
        if match.group(2) != "1":
            continue
        steps.append(
            (
                datetime.datetime.strptime(match.group(1), WRF_TIME),
                float(match.group(3)),
            )
        )
    if not steps:
        raise UnsupportedOutput("No 'Timing for main' lines for domain 1 yet.")
    first_time, first_seconds = steps[0]
    # The first step's model time is one time step past the start.
    step_length = (steps[1][0] - first_time).total_seconds() if len(steps) > 1 else 0
    start = first_time - datetime.timedelta(seconds=step_length)
    wall, points = 0.0, [[0.0, 0.0]]
    for model_time, seconds in steps:
        wall += seconds
        points.append(
            [
                round(wall / 3600, 4),
                round((model_time - start).total_seconds() / 3600, 3),
            ]
        )
    simulated = points[-1][1]
    wall_hours = points[-1][0]
    summary = {
        "start": start.strftime(WRF_TIME),
        "reached": steps[-1][0].strftime(WRF_TIME),
        "simulated_hours": simulated,
        "wall_hours": round(wall_hours, 3),
        "speed": round(simulated / wall_hours, 2) if wall_hours else None,
        "forecast_hours": forecast_hours,
        "percent": None,
        "eta_hours": None,
    }
    if forecast_hours and wall_hours and simulated:
        summary["percent"] = round(min(simulated / forecast_hours, 1) * 100, 1)
        remaining = max(forecast_hours - simulated, 0)
        summary["eta_hours"] = round(remaining / (simulated / wall_hours), 2)
    return OutputSeries(
        kind="wrf_rsl",
        title="WRF run progress",
        x_label="Wall-clock hours",
        y_label="Forecast hours",
        series=[{"name": "Forecast time reached", "points": _decimate(points)}],
        summary=summary,
    )


NAMELIST_RUN = re.compile(r"^\s*run_(days|hours|minutes)\s*=\s*(\d+)", re.MULTILINE)


def forecast_hours_from_namelist(text: str) -> float | None:
    """run_days/run_hours/run_minutes from WRF's namelist.input (&time_control)."""
    factors = {"days": 24, "hours": 1, "minutes": 1 / 60}
    total = sum(
        int(value) * factors[unit] for unit, value in NAMELIST_RUN.findall(text)
    )
    return total or None


def parse(path: str, text: str, forecast_hours: float | None = None) -> OutputSeries:
    kind = kind_for_path(path)
    if kind == "xvg":
        return parse_xvg(text)
    if kind == "csv":
        return parse_csv(text, path)
    if kind == "wrf_rsl":
        return parse_wrf_rsl(text, forecast_hours)
    raise UnsupportedOutput(
        "Plots are available for GROMACS .xvg, CSV/TSV tables and WRF rsl logs."
    )
