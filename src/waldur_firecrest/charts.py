"""Chart specs for the HPC workspace figures (see chat.chart_spec).

The Insights tab and the assistant render the same specs, so a chart looks
and reads the same wherever it appears.
"""

import datetime

from django.utils import timezone

from waldur_mastermind.chat.chart_spec import chart, series

OUTCOME_SERIES = (
    ("completed", "Completed", "success"),
    ("failed", "Failed", "danger"),
    ("timeout", "Timed out", "warning"),
    ("cancelled", "Cancelled", "neutral"),
)
NODE_ROW_WIDTH = 16


def insights_path(resource_uuid: str) -> str:
    return f"/resource-details/{resource_uuid}/?tab=hpc-insights"


def _ms(value) -> int:
    """Epoch milliseconds from epoch seconds or an ISO date."""
    if isinstance(value, str):
        day = datetime.date.fromisoformat(value)
        moment = timezone.make_aware(datetime.datetime.combine(day, datetime.time()))
        return int(moment.timestamp() * 1000)
    return int(value * 1000)


def _today_ms() -> int:
    return _ms(timezone.localdate().isoformat())


# Allocation


def allocation_burn(component: dict, link: str | None = None) -> dict:
    unit = component["unit"] or component["name"]
    used = [[_ms(p["date"]), p["value"]] for p in component["cumulative"]]
    items = [
        series(
            "Used",
            used,
            tone="info",
            labels=[
                f"{p['value']:,.0f} {unit} by {p['date']}"
                for p in component["cumulative"]
            ],
        )
    ]
    limit, end = component["limit"], component["period_end"]
    annotations = []
    if limit:
        annotations.append(
            {
                "type": "line_y",
                "value": limit,
                "label": f"Limit {limit:,.0f}",
                "tone": "danger",
            }
        )
        if end:
            items.append(
                series(
                    "Even pace",
                    [[_ms(component["period_start"]), 0], [_ms(end), limit]],
                    style="dotted",
                    tone="neutral",
                )
            )
    if component["daily_rate"] and used:
        last_x, last_y = used[-1]
        if component["runs_out"] and limit:
            target = [_ms(component["runs_out"]), limit]
        elif end:
            days = (datetime.date.fromisoformat(end) - timezone.localdate()).days
            target = [_ms(end), last_y + component["daily_rate"] * max(days, 0)]
        else:
            target = None
        if target:
            items.append(
                series(
                    "Forecast",
                    [[last_x, last_y], target],
                    style="dashed",
                    tone="warning"
                    if component["runs_out_before_period_end"]
                    else "neutral",
                )
            )
    annotations.append({"type": "line_x", "value": _today_ms(), "label": "Today"})
    if component["runs_out"]:
        annotations.append(
            {
                "type": "line_x",
                "value": _ms(component["runs_out"]),
                "label": f"Runs out {component['runs_out']}",
                "tone": "warning",
            }
        )
    if limit:
        subtitle = (
            f"{component['consumed']:,.0f} of {limit:,.0f} {unit} used "
            f"({component['percent']}%)"
        )
    else:
        subtitle = f"{component['consumed']:,.0f} {unit} used"
    if component["runs_out"]:
        subtitle += f"; at the recent pace it runs out on {component['runs_out']}"
    return chart(
        "area",
        f"{component['name']} against the allocation",
        x={"type": "time"},
        y={"type": "value", "label": unit, "min": 0},
        series=items,
        subtitle=subtitle,
        annotations=annotations,
        link={"label": "Open insights", "url": link} if link else None,
        empty_message="No usage has been reported for this allocation yet.",
    )


def usage_by_user(component: dict, top: int = 6) -> dict:
    months = sorted(
        {m["month"] for user in component["users"] for m in user["monthly"]}
    )
    users = component["users"][:top]
    rest = component["users"][top:]
    items = []
    for user in users:
        values = {m["month"]: m["value"] for m in user["monthly"]}
        items.append(
            series(
                user["username"],
                [[i, values.get(month, 0)] for i, month in enumerate(months)],
            )
        )
    if rest:
        totals = [
            sum(
                m["value"]
                for user in rest
                for m in user["monthly"]
                if m["month"] == month
            )
            for month in months
        ]
        items.append(
            series(
                "others", [[i, value] for i, value in enumerate(totals)], tone="neutral"
            )
        )
    unit = component["unit"] or component["name"]
    return chart(
        "stacked_bar",
        f"{component['name']} by user",
        x={"type": "category", "categories": [month[:7] for month in months]},
        y={"type": "value", "label": unit, "min": 0},
        series=items,
        empty_message="No per-user usage has been reported.",
    )


# Jobs


def outcomes(stats: dict) -> dict:
    days = stats["outcomes_per_day"]
    return chart(
        "stacked_bar",
        "Job outcomes per day",
        x={"type": "category", "categories": [d["date"][5:] for d in days]},
        y={"type": "value", "label": "jobs", "min": 0},
        series=[
            series(label, [[i, d[key]] for i, d in enumerate(days)], tone=tone)
            for key, label, tone in OUTCOME_SERIES
        ],
        subtitle=f"Last {stats['days']} days",
        empty_message="No jobs finished in this period.",
    )


def walltime_fit(stats: dict) -> dict:
    points = stats["walltime"]
    items = []
    for key, label, tone in OUTCOME_SERIES:
        group = [p for p in points if p["outcome"] == key]
        if not group:
            continue
        items.append(
            series(
                label,
                [
                    [
                        round(p["requested_minutes"] / 60, 3),
                        max(round(p["used_minutes"] / 60, 3), 0.01),
                    ]
                    for p in group
                ],
                tone=tone,
                labels=[
                    f"{p['name']} #{p['job_id']}: used {p['used_minutes']:.0f} of "
                    f"{p['requested_minutes']} min"
                    for p in group
                ],
                ids=[str(p["job_id"]) for p in group],
            )
        )
    if points:
        top = max(p["requested_minutes"] for p in points) / 60
        items.append(
            series(
                "Used the whole limit",
                [[0.01, 0.01], [top, top]],
                style="dashed",
                tone="neutral",
            )
        )
    return chart(
        "scatter",
        "Time limit against time used",
        x={"type": "log", "label": "Requested", "unit": "h"},
        y={"type": "log", "label": "Used", "unit": "h"},
        series=items,
        subtitle="Points far below the dashed line asked for much more time than they used.",
        empty_message="No finished jobs with a time limit in this period.",
    )


def efficiency(stats: dict) -> dict:
    points = stats["efficiency"]
    with_memory = [p for p in points if p["memory_efficiency"] is not None]
    return chart(
        "scatter",
        "CPU and memory efficiency",
        x={
            "type": "value",
            "label": "CPU efficiency",
            "unit": "%",
            "min": 0,
            "max": 100,
        },
        y={
            "type": "value",
            "label": "Peak memory",
            "unit": "%",
            "min": 0,
            "max": 100,
        },
        series=[
            series(
                "Jobs",
                [
                    [
                        round(p["cpu_efficiency"] * 100, 1),
                        round(p["memory_efficiency"] * 100, 1),
                    ]
                    for p in with_memory
                ],
                tone="info",
                labels=[
                    f"{p['name']} #{p['job_id']}: CPU {p['cpu_efficiency']:.0%}, "
                    f"memory {p['memory_efficiency']:.0%}"
                    for p in with_memory
                ],
                ids=[str(p["job_id"]) for p in with_memory],
            )
        ],
        annotations=[
            {
                "type": "area_x",
                "value": 0,
                "value_end": 40,
                "label": "Low CPU use",
                "tone": "warning",
            }
        ],
        empty_message="The cluster does not report CPU time and peak memory for these jobs.",
    )


def queue_wait(stats: dict) -> dict:
    rows = stats["wait_by_partition"]
    return chart(
        "bar",
        "Queue wait by partition",
        x={"type": "category", "categories": [r["partition"] for r in rows]},
        y={"type": "value", "label": "wait", "unit": "min", "min": 0},
        series=[
            series(
                "Median",
                [
                    [i, round(r["median_wait_seconds"] / 60, 1)]
                    for i, r in enumerate(rows)
                ],
                tone="info",
            ),
            series(
                "90th percentile",
                [[i, round(r["p90_wait_seconds"] / 60, 1)] for i, r in enumerate(rows)],
                tone="neutral",
            ),
        ],
        subtitle=f"Time from submission to start, last {stats['days']} days",
        empty_message="No submission times in this period.",
    )


def job_timeline(stats: dict) -> dict:
    rows = stats["timeline"]
    categories = [f"{r['job_id']} {r['name']}" for r in rows]
    items = []
    for key, label, tone in (*OUTCOME_SERIES, ("running", "Running", "info")):
        group = [(i, r) for i, r in enumerate(rows) if r["outcome"] == key]
        if not group:
            continue
        items.append(
            series(
                label,
                [
                    [
                        i,
                        _ms(r["start"]),
                        _ms(r["end"]),
                        _ms(r["limit_end"]) if r["limit_end"] else None,
                    ]
                    for i, r in group
                ],
                tone=tone,
                labels=[
                    f"{r['name']} #{r['job_id']} on {r['partition']}: {r['state'].lower()}"
                    for _, r in group
                ],
                ids=[str(r["job_id"]) for _, r in group],
            )
        )
    return chart(
        "timeline",
        "Recent jobs",
        x={"type": "time"},
        y={"type": "category", "categories": categories},
        series=items,
        subtitle="Bars run from start to end; the faint part is time limit left unused.",
        empty_message="No jobs started in this period.",
    )


# Cluster


def node_map(cluster: dict, partition: str = "") -> dict:
    partitions = (
        [partition] if partition else [p["name"] for p in cluster["partitions"]]
    )
    categories, load, unavailable = [], [], []
    load_labels, unavailable_labels = [], []
    for name in partitions:
        nodes = [n for n in cluster["nodes"] if name in n["partitions"]]
        for offset in range(0, len(nodes), NODE_ROW_WIDTH):
            row = len(categories)
            categories.append(name if offset == 0 else "")
            for column, node in enumerate(nodes[offset : offset + NODE_ROW_WIDTH]):
                if node["state"] in ("DOWN", "DRAIN", "MAINTENANCE"):
                    unavailable.append([column, row, None])
                    unavailable_labels.append(
                        f"{node['name']}: {node['state'].lower()}"
                        + (f" ({node['reason']})" if node["reason"] else "")
                    )
                else:
                    value = (
                        node["load_percent"] if node["load_percent"] is not None else 0
                    )
                    load.append([column, row, value])
                    load_labels.append(
                        f"{node['name']}: {node['state'].lower()}, load {value:.0f}%"
                    )
    items = [series("CPU load", load, labels=load_labels)]
    if unavailable:
        items.append(
            series("Unavailable", unavailable, tone="danger", labels=unavailable_labels)
        )
    return chart(
        "heatmap",
        f"Nodes of {partition}" if partition else "Nodes by partition",
        x={
            "type": "category",
            "categories": [str(i + 1) for i in range(NODE_ROW_WIDTH)],
        },
        y={"type": "category", "categories": categories},
        series=items,
        subtitle="Colour shows CPU load; red cells are drained or down.",
        empty_message="The cluster reports no nodes.",
    )


def reservations(cluster: dict) -> dict:
    """Announced maintenance (Waldur) next to the cluster's reservations (Slurm)."""
    every = sorted(p["name"] for p in cluster["partitions"])

    def scope(row) -> str:
        if row["partitions"] == every and len(every) > 1:
            return "all nodes"
        return ", ".join(row["partitions"]) or row.get("node_list", "")

    categories: list[str] = []

    def category(label: str) -> int:
        if label not in categories:
            categories.append(label)
        return categories.index(label)

    announced = cluster.get("announcements", [])
    groups = [
        (
            "Announced (Waldur)",
            "warning",
            [
                (
                    category(f"{scope(a)} · announced"),
                    a,
                    f"{a['name']}: {a['impact'].lower()}"
                    + (
                        "; not held on the cluster yet"
                        if a["reservation_in_place"] is False
                        else ""
                    ),
                )
                for a in announced
            ],
        ),
        (
            "Maintenance (cluster)",
            "danger",
            [
                (
                    category(scope(r)),
                    r,
                    r["name"]
                    + ("; not announced in Waldur" if r["announced"] is False else ""),
                )
                for r in cluster["reservations"]
                if r["maintenance"]
            ],
        ),
        (
            "Reservation",
            "info",
            [
                (category(scope(r)), r, r["name"])
                for r in cluster["reservations"]
                if not r["maintenance"]
            ],
        ),
    ]
    items = [
        series(
            label,
            [[index, _ms(row["start"]), _ms(row["end"])] for index, row, _ in group],
            tone=tone,
            labels=[text for _, _, text in group],
        )
        for label, tone, group in groups
        if group
    ]
    return chart(
        "timeline",
        "Maintenance and reservations",
        x={"type": "time"},
        y={"type": "category", "categories": categories},
        series=items,
        subtitle="Announced maintenance from Waldur next to what the cluster holds.",
        annotations=[
            {
                "type": "line_x",
                "value": timezone.now().timestamp() * 1000,
                "label": "Now",
            }
        ],
        empty_message="No upcoming maintenance or reservations.",
    )


# Output files


def output_series(data: dict) -> dict:
    annotations = []
    forecast = (
        data["summary"].get("forecast_hours") if data["kind"] == "wrf_rsl" else None
    )
    if forecast:
        annotations.append(
            {"type": "line_y", "value": forecast, "label": f"+{forecast:g} h forecast"}
        )
    subtitle = ""
    if data["kind"] == "wrf_rsl":
        summary = data["summary"]
        subtitle = f"Reached {summary['reached']}"
        if summary.get("percent") is not None:
            subtitle += f" ({summary['percent']}%), about {summary['eta_hours']:.1f} h of wall time left"
    items, secondary = _split_by_magnitude(data["series"])
    return chart(
        "line",
        data["title"],
        x={"type": "value", "label": data["x_label"]},
        y={
            "type": "value",
            "label": data["series"][0]["name"] if secondary else data["y_label"],
        },
        y2={"type": "value", "label": secondary} if secondary else None,
        series=items,
        subtitle=subtitle,
        annotations=annotations,
    )


def _magnitude(points) -> float:
    values = [abs(y) for _, y in points if isinstance(y, (int, float))]
    return max(values) if values else 0


def _split_by_magnitude(raw: list[dict]) -> tuple[list[dict], str]:
    """Series far from the first one's scale (e.g. K next to kJ/mol) go right.

    Returns the series and the secondary axis label ("" when one axis fits).
    """
    if len(raw) < 2:
        return [series(s["name"], s["points"]) for s in raw], ""
    base = _magnitude(raw[0]["points"]) or 1
    items, right = [], []
    for index, s in enumerate(raw):
        ratio = (_magnitude(s["points"]) or 1) / base
        far = index > 0 and (ratio > 10 or ratio < 0.1)
        items.append(
            series(s["name"], s["points"], y_axis="secondary" if far else None)
        )
        if far:
            right.append(s["name"])
    return items, ", ".join(right)
