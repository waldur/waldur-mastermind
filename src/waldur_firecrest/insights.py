"""Figures about an allocation, its jobs and its cluster, for charts and the assistant.

Everything here is computed from what the workspace already fetches:
Waldur's usage records for the allocation, FirecREST's job list and the
cluster's nodes and reservations. Functions take plain data and return
plain dicts, so the REST views and the AI assistant tools share them.

FirecREST job dicts use its camelCase names; times are epoch seconds and
``time.limit`` is in seconds (FirecREST converts Slurm's minutes).
"""

import datetime
import math
import statistics
from collections import Counter, defaultdict

from dateutil.relativedelta import relativedelta
from django.db.models import Sum
from django.utils import timezone

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ImpactLevel, LimitPeriods

MIB = 1024 * 1024

FAILED_STATES = ("FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE")
OUTCOMES = ("completed", "failed", "timeout", "cancelled")
ACTIVE_STATES = ("PENDING", "RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED")

# Node states, worst first: the first one a node has decides its colour.
NODE_STATE_ORDER = (
    "DOWN",
    "DRAIN",
    "MAINTENANCE",
    "RESERVED",
    "ALLOCATED",
    "MIXED",
    "COMPLETING",
    "IDLE",
)

OUTAGE_IMPACTS = (ImpactLevel.FULL_OUTAGE, ImpactLevel.PARTIAL_OUTAGE)

# Limit suggestions round up to one of these (minutes).
LIMIT_STEPS = (15, 30, 60, 120, 240, 360, 480, 720, 960, 1440, 2160, 2880)


def outcome(job: dict) -> str | None:
    """completed / failed / timeout / cancelled, or None for an active job."""
    state = _state(job)
    if state == "COMPLETED":
        return "completed"
    if state.startswith("CANCELLED"):
        return "cancelled"
    if state == "TIMEOUT":
        return "timeout"
    if state in FAILED_STATES or state == "PREEMPTED":
        return "failed"
    return None


def _state(job: dict) -> str:
    return ((job.get("status") or {}).get("state") or "").upper()


def _ratio(numerator, denominator) -> float | None:
    if not numerator or not denominator or denominator <= 0:
        return None
    return round(min(numerator / denominator, 1.0), 3)


def job_metrics(job: dict) -> dict:
    """Wait, CPU time, efficiency and core-hours a job record allows."""
    times = job.get("time") or {}
    submitted, start = times.get("submission"), times.get("start")
    elapsed = times.get("elapsed")
    cpus = job.get("allocationCpus")
    finished = outcome(job) is not None
    wait = None
    if submitted and start and _state(job) != "PENDING" and start >= submitted:
        wait = int(start - submitted)
    cpu_time = times.get("totalCpu")
    requested_memory = job.get("requestedMemory")
    max_rss = job.get("maxRss")
    return {
        "wait_seconds": wait,
        "cpu_time_seconds": cpu_time,
        "cpu_hours": round(cpus * elapsed / 3600, 2) if cpus and elapsed else None,
        "cpu_efficiency": _ratio(cpu_time, (cpus or 0) * (elapsed or 0))
        if finished
        else None,
        "memory_efficiency": _ratio(max_rss, (requested_memory or 0) * MIB)
        if finished
        else None,
        "max_rss_mb": round(max_rss / MIB) if max_rss else None,
        **gpu_and_energy_metrics(job),
    }


def gpu_and_energy_metrics(job: dict) -> dict:
    """GPU-hours and energy, where the site's accounting records them.

    Energy is FireCREST's ``consumedEnergy``, in joules. GPU use and GPU
    memory come from slurm-monitor (job performance), not from FireCREST.
    """
    elapsed = (job.get("time") or {}).get("elapsed")
    gpus = job.get("allocationGpus")
    energy = job.get("consumedEnergy")
    return {
        "gpu_hours": round(gpus * elapsed / 3600, 2) if gpus and elapsed else None,
        "energy_kwh": round(energy / 3_600_000, 2) if energy else None,
        "mean_power_w": round(energy / elapsed) if energy and elapsed else None,
    }


def with_metrics(job: dict) -> dict:
    return {**job, "metrics": job_metrics(job)}


def _local_date(timestamp) -> str:
    moment = datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC)
    return timezone.localtime(moment).date().isoformat()


def _percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(index, 0)]


def suggested_limit(used_seconds: list[int]) -> int:
    """A time limit (minutes) covering the longest run with a 25% margin."""
    needed = max(used_seconds) * 1.25 / 60
    return next((step for step in LIMIT_STEPS if step >= needed), LIMIT_STEPS[-1])


def job_stats(jobs: list[dict], days: int = 14, now: float | None = None) -> dict:
    """Outcomes, time-limit fit, efficiency and queue waits over *days*."""
    now = now or timezone.now().timestamp()
    since = now - days * 86400
    jobs = [with_metrics(job) if "metrics" not in job else job for job in jobs]
    finished = [
        job
        for job in jobs
        if outcome(job) and ((job.get("time") or {}).get("end") or 0) >= since
    ]
    active = [job for job in jobs if _state(job) in ACTIVE_STATES]

    totals = Counter(outcome(job) for job in finished)
    by_day = defaultdict(Counter)
    first_day = datetime.date.fromisoformat(_local_date(since))
    for offset in range(days + 1):
        by_day[(first_day + datetime.timedelta(days=offset)).isoformat()]
    for job in finished:
        by_day[_local_date(job["time"]["end"])][outcome(job)] += 1
    outcomes_per_day = [
        {"date": day, **{name: counts[name] for name in OUTCOMES}}
        for day, counts in sorted(by_day.items())
        if day <= _local_date(now)
    ]

    exit_codes = defaultdict(list)
    for job in finished:
        if outcome(job) == "failed":
            exit_codes[(job.get("status") or {}).get("exitCode")].append(job)
    failures = [
        {
            "exit_code": code,
            "count": len(items),
            "names": sorted({job.get("name") or "" for job in items}),
            "latest_job_id": max(items, key=lambda j: j["time"]["end"]).get("jobId"),
        }
        for code, items in sorted(
            exit_codes.items(), key=lambda item: len(item[1]), reverse=True
        )
    ]

    walltime = []
    by_name = defaultdict(list)
    for job in finished:
        times = job.get("time") or {}
        limit, elapsed = times.get("limit"), times.get("elapsed")
        if not limit or elapsed is None:
            continue
        point = {
            "job_id": job.get("jobId"),
            "name": job.get("name"),
            "partition": job.get("partition"),
            "outcome": outcome(job),
            "requested_minutes": round(limit / 60),
            "used_minutes": round(elapsed / 60, 1),
            "ratio": round(elapsed / limit, 3),
        }
        walltime.append(point)
        if point["outcome"] == "completed":
            by_name[job.get("name")].append((elapsed, limit))

    efficiency = [
        {
            "job_id": job.get("jobId"),
            "name": job.get("name"),
            "cpu_efficiency": job["metrics"]["cpu_efficiency"],
            "memory_efficiency": job["metrics"]["memory_efficiency"],
            "cpu_hours": job["metrics"]["cpu_hours"],
        }
        for job in finished
        if job["metrics"]["cpu_efficiency"] is not None
    ]

    waits = defaultdict(list)
    for job in jobs:
        start = (job.get("time") or {}).get("start") or 0
        if job["metrics"]["wait_seconds"] is not None and start >= since:
            waits[job.get("partition") or ""].append(job["metrics"]["wait_seconds"])
    wait_by_partition = [
        {
            "partition": partition,
            "jobs": len(values),
            "median_wait_seconds": int(statistics.median(values)),
            "p90_wait_seconds": int(_percentile(values, 0.9)),
        }
        for partition, values in sorted(waits.items())
    ]

    cpu_hours_by_day = defaultdict(float)
    for job in finished:
        if job["metrics"]["cpu_hours"]:
            cpu_hours_by_day[_local_date(job["time"]["end"])] += job["metrics"][
                "cpu_hours"
            ]

    recent = sorted(
        (job for job in jobs if ((job.get("time") or {}).get("start") or 0) >= since),
        key=lambda job: job["time"]["start"],
        reverse=True,
    )[:30]
    timeline = [
        {
            "job_id": job.get("jobId"),
            "name": job.get("name"),
            "state": _state(job),
            "outcome": outcome(job) or "running",
            "partition": job.get("partition"),
            "submit": job["time"].get("submission"),
            "start": job["time"]["start"],
            "end": job["time"].get("end")
            if outcome(job)
            else max(now, job["time"]["start"]),
            "limit_end": job["time"]["start"] + job["time"]["limit"]
            if job["time"].get("limit")
            else None,
        }
        for job in recent
    ]

    return {
        "days": days,
        "since": int(since),
        "until": int(now),
        "totals": {
            **{name: totals[name] for name in OUTCOMES},
            "running": sum(1 for job in active if _state(job) != "PENDING"),
            "pending": sum(1 for job in active if _state(job) == "PENDING"),
        },
        "cpu_hours": round(sum(cpu_hours_by_day.values()), 1),
        "outcomes_per_day": outcomes_per_day,
        "failures": failures,
        "walltime": walltime,
        "efficiency": efficiency,
        "wait_by_partition": wait_by_partition,
        "cpu_hours_by_day": [
            {"date": day, "cpu_hours": round(value, 1)}
            for day, value in sorted(cpu_hours_by_day.items())
        ],
        "timeline": timeline,
        "suggestions": _suggestions(by_name, efficiency),
    }


def _suggestions(by_name: dict, efficiency: list[dict]) -> list[dict]:
    """Right-sizing hints for job names with a repeated pattern."""
    suggestions = []
    for name, runs in sorted(by_name.items()):
        if len(runs) < 3:
            continue
        ratios = [elapsed / limit for elapsed, limit in runs]
        median_ratio = statistics.median(ratios)
        if median_ratio >= 0.3:
            continue
        current = round(statistics.median(limit for _, limit in runs) / 60)
        proposed = suggested_limit([elapsed for elapsed, _ in runs])
        if proposed >= current:
            continue
        suggestions.append(
            {
                "kind": "time_limit",
                "name": name,
                "jobs": len(runs),
                "median_used_percent": round(median_ratio * 100),
                "current_limit_minutes": current,
                "suggested_limit_minutes": proposed,
                "message": f"{name}: {len(runs)} runs used a median "
                f"{round(median_ratio * 100)}% of their {format_minutes(current)} "
                f"limit. A {format_minutes(proposed)} limit covers the longest run "
                "with a 25% margin and lets the scheduler start them sooner.",
            }
        )

    low = defaultdict(list)
    for point in efficiency:
        low[point["name"]].append(point["cpu_efficiency"])
    for name, values in sorted(low.items()):
        median_efficiency = statistics.median(values)
        if len(values) >= 3 and median_efficiency < 0.4:
            suggestions.append(
                {
                    "kind": "cpu_efficiency",
                    "name": name,
                    "jobs": len(values),
                    "median_cpu_efficiency_percent": round(median_efficiency * 100),
                    "message": f"{name}: {len(values)} runs kept a median "
                    f"{round(median_efficiency * 100)}% of their CPUs busy. "
                    "Requesting fewer cores (or checking that the program "
                    "really runs in parallel) would save allocation.",
                }
            )
    return suggestions


def format_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440} d" if minutes > 1440 else "24 h"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min"


# Cluster


def node_state(node: dict) -> str:
    states = {str(state).upper() for state in node.get("state") or []}
    return next((s for s in NODE_STATE_ORDER if s in states), "UNKNOWN")


def cluster_load(
    nodes: list[dict],
    reservations: list[dict],
    announcements: list[dict] | None = None,
    now: float | None = None,
) -> dict:
    """Node map, per-partition summary, reservations and announced maintenance.

    *announcements* are the provider's published maintenance for the offering
    (see ``announcement_rows``). Each is matched with the cluster's
    maintenance reservations both ways, so a page can say whether an
    announced outage is actually held by the scheduler, and whether a
    maintenance reservation was ever announced.
    """
    now = now or timezone.now().timestamp()
    node_rows = []
    summary = defaultdict(lambda: Counter())
    loads = defaultdict(list)
    for node in sorted(nodes, key=lambda n: n.get("name") or ""):
        cpus = node.get("cpus") or 0
        # The load average (FireCREST >= 2.6.2 divides slurmrestd's x 100),
        # as a share of the node's CPUs.
        load = node.get("cpuLoad")
        load_percent = (
            round(min(load * 100 / cpus, 100.0), 1)
            if load is not None and cpus
            else None
        )
        state = node_state(node)
        row = {
            "name": node.get("name"),
            "partitions": node.get("partitions") or [],
            "state": state,
            "cpus": cpus,
            "alloc_cpus": node.get("allocCpus"),
            "load_percent": load_percent,
            "free_memory_mb": node.get("freeMemory"),
            "reason": node.get("reason") or "",
        }
        node_rows.append(row)
        for partition in row["partitions"]:
            summary[partition][state] += 1
            summary[partition]["total"] += 1
            if load_percent is not None and state not in ("DOWN", "DRAIN"):
                loads[partition].append(load_percent)

    partitions = [
        {
            "name": name,
            "total": counts["total"],
            "idle": counts["IDLE"],
            "mixed": counts["MIXED"],
            "allocated": counts["ALLOCATED"] + counts["COMPLETING"],
            "unavailable": counts["DOWN"] + counts["DRAIN"] + counts["MAINTENANCE"],
            "reserved": counts["RESERVED"],
            "average_load_percent": round(statistics.mean(loads[name]), 1)
            if loads[name]
            else None,
        }
        for name, counts in sorted(summary.items())
    ]

    reservation_rows = []
    for item in reservations:
        start, end = item.get("startTime"), item.get("endTime")
        if not start or not end or end < now:
            continue
        reservation_rows.append(
            {
                "name": item.get("name"),
                "start": start,
                "end": end,
                "node_list": item.get("nodeList") or "",
                "node_count": item.get("nodeCount"),
                "maintenance": bool(item.get("maintenance")),
                "active": start <= now < end,
                "partitions": _reservation_partitions(item, node_rows),
            }
        )
    reservation_rows.sort(key=lambda r: r["start"])
    every = sorted(summary)
    announcement_rows = []
    for announcement in announcements or []:
        if announcement["end"] < now:
            continue
        row = {**announcement, "partitions": announcement["partitions"] or every}
        if row["reserve"]:
            row["reservation_in_place"] = any(
                r["maintenance"]
                and (
                    r["name"] == row["expected_reservation"]
                    or _covers(r, row["start"], row["end"], row["partitions"])
                )
                for r in reservation_rows
            )
        else:
            row["reservation_in_place"] = None
        announcement_rows.append(row)
    for reservation in reservation_rows:
        reservation["announced"] = (
            any(
                reservation["name"] == a["expected_reservation"]
                or _overlaps(reservation, a["start"], a["end"], a["partitions"])
                for a in announcement_rows
            )
            if reservation["maintenance"]
            else None
        )
    return {
        "announcements": announcement_rows,
        "nodes": node_rows,
        "partitions": partitions,
        "reservations": reservation_rows,
    }


def expand_hostlist(expression: str) -> set[str]:
    """node[001-003,007],gpu01 -> {node001, node002, node003, node007, gpu01}."""
    names, depth, current = [], 0, ""
    for char in expression:
        if char == "," and depth == 0:
            names.append(current)
            current = ""
            continue
        depth += {"[": 1, "]": -1}.get(char, 0)
        current += char
    names.append(current)
    hosts = set()
    for name in filter(None, (n.strip() for n in names)):
        if "[" not in name:
            hosts.add(name)
            continue
        prefix, _, rest = name.partition("[")
        ranges, _, suffix = rest.partition("]")
        for part in ranges.split(","):
            first, _, last = part.partition("-")
            last = last or first
            width = len(first)
            for number in range(int(first), int(last) + 1):
                hosts.add(f"{prefix}{number:0{width}d}{suffix}")
    return hosts


def _reservation_partitions(item: dict, node_rows: list[dict]) -> list[str]:
    node_list = (item.get("nodeList") or "").strip()
    every = sorted({p for row in node_rows for p in row["partitions"]})
    if not node_list or node_list.upper() == "ALL":
        return every
    try:
        hosts = expand_hostlist(node_list)
    except ValueError:
        return every
    return sorted(
        {p for row in node_rows if row["name"] in hosts for p in row["partitions"]}
    )


def _overlaps(reservation: dict, start, end, partitions) -> bool:
    return (
        reservation["start"] < end
        and reservation["end"] > start
        and bool(set(reservation["partitions"]) & set(partitions))
    )


def _covers(reservation: dict, start, end, partitions) -> bool:
    return (
        reservation["start"] <= start
        and reservation["end"] >= end
        and set(partitions) <= set(reservation["partitions"])
    )


def announcement_rows(maintenance_offerings, expected_reservation, reserve) -> list:
    """Published maintenance of an offering, shaped like the cluster's data.

    *expected_reservation(maintenance)* names the reservation the site agent
    would create; *reserve(row)* says whether one is due at all (opt-in and
    an outage the scheduler should hold).
    """
    rows = []
    for row in maintenance_offerings:
        maintenance = row.maintenance
        due = reserve(row)
        rows.append(
            {
                "uuid": maintenance.uuid.hex,
                "name": maintenance.name,
                "message": maintenance.message,
                "state": maintenance.get_state_display(),
                "impact": row.get_impact_level_display(),
                "outage": row.impact_level in OUTAGE_IMPACTS,
                "impact_description": row.impact_description,
                "start": int(maintenance.scheduled_start.timestamp()),
                "end": int(maintenance.scheduled_end.timestamp()),
                "partitions": list(row.affected_partitions or []),
                "reserve": due,
                "expected_reservation": expected_reservation(maintenance)
                if due
                else "",
            }
        )
    return rows


def maintenance_conflicts(
    reservations: list[dict],
    partition: str,
    time_limit_minutes: int,
    now=None,
    announcements: list[dict] | None = None,
) -> list[str]:
    """Warnings for maintenance windows a job started now could run into.

    Announced outages the cluster does not hold yet warn too: the scheduler
    will not wait for them, so the job may be cut off.
    """
    now = now or timezone.now().timestamp()
    end = now + time_limit_minutes * 60
    warnings = []
    for announcement in announcements or []:
        if not announcement["outage"] or announcement["reservation_in_place"]:
            continue
        if partition and partition not in announcement["partitions"]:
            continue
        if announcement["start"] < end and announcement["end"] > now:
            starts = timezone.localtime(
                datetime.datetime.fromtimestamp(announcement["start"], tz=datetime.UTC)
            )
            warnings.append(
                f"Announced maintenance '{announcement['name']}' "
                f"({announcement['impact'].lower()}) starts {starts:%a %d %b %H:%M}. "
                "The cluster does not hold it back yet, so a job still running "
                "then may be stopped."
            )
    for reservation in reservations:
        if not reservation["maintenance"]:
            continue
        if partition and partition not in reservation["partitions"]:
            continue
        if reservation["start"] < end and reservation["end"] > now:
            starts = timezone.localtime(
                datetime.datetime.fromtimestamp(reservation["start"], tz=datetime.UTC)
            )
            warnings.append(
                f"Maintenance '{reservation['name']}' starts {starts:%a %d %b %H:%M}. "
                "A job that cannot finish before it waits until it is over; "
                "a shorter time limit may let it run first."
            )
    return warnings


# Allocation


def _period_bounds(component, resource, today: datetime.date):
    period = component.limit_period
    if period == LimitPeriods.MONTH:
        start = today.replace(day=1)
        return start, start + relativedelta(months=1, days=-1)
    if period == LimitPeriods.QUARTERLY:
        start = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
        return start, start + relativedelta(months=3, days=-1)
    if period == LimitPeriods.ANNUAL:
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    # TOTAL: over the allocation's life.
    first_usage = (
        marketplace_models.ComponentUsage.objects.filter(
            resource=resource, component=component
        )
        .order_by("billing_period")
        .values_list("billing_period", flat=True)
        .first()
    )
    start = min(
        filter(None, [first_usage, timezone.localtime(resource.created).date()])
    )
    return start, resource.end_date


def allocation_usage(resource, today: datetime.date | None = None) -> dict:
    """Consumption against each limited component, with a run-out forecast."""
    today = today or timezone.localdate()
    limits = resource.limits or {}
    components = []
    for component in resource.offering.components.all().order_by("name"):
        if component.billing_type not in ("usage", "limit") and component.type not in (
            limits
        ):
            continue
        limit = limits.get(component.type)
        start, end = _period_bounds(component, resource, today)
        usages = marketplace_models.ComponentUsage.objects.filter(
            resource=resource,
            component=component,
            billing_period__gte=start.replace(day=1),
        ).order_by("billing_period")
        if end:
            usages = usages.filter(billing_period__lte=end)
        monthly = [(usage.billing_period, float(usage.usage)) for usage in usages]
        if not monthly and limit is None:
            continue

        consumed = sum(value for _, value in monthly)
        cumulative, running = [{"date": start.isoformat(), "value": 0.0}], 0.0
        for period, value in monthly:
            running += value
            month_end = period + relativedelta(months=1, days=-1)
            point_date = min(month_end, today)
            cumulative.append(
                {"date": point_date.isoformat(), "value": round(running, 2)}
            )

        rate = _daily_rate(monthly, today)
        runs_out = None
        if limit and rate and consumed < float(limit):
            runs_out = today + datetime.timedelta(
                days=math.ceil((float(limit) - consumed) / rate)
            )
        per_user = (
            marketplace_models.ComponentUserUsage.objects.filter(
                component_usage__in=usages
            )
            .values("username", "component_usage__billing_period")
            .annotate(total=Sum("usage"))
            .order_by("component_usage__billing_period")
        )
        users = defaultdict(lambda: defaultdict(float))
        for row in per_user:
            users[row["username"]][
                row["component_usage__billing_period"].isoformat()
            ] += float(row["total"])

        components.append(
            {
                "type": component.type,
                "name": component.name,
                "unit": component.measured_unit,
                "limit": float(limit) if limit is not None else None,
                "limit_period": component.limit_period,
                "period_start": start.isoformat(),
                "period_end": end.isoformat() if end else None,
                "consumed": round(consumed, 2),
                "percent": round(consumed / float(limit) * 100, 1) if limit else None,
                "daily_rate": round(rate, 2) if rate else None,
                "runs_out": runs_out.isoformat() if runs_out else None,
                "runs_out_before_period_end": bool(runs_out and end and runs_out < end),
                "cumulative": cumulative,
                "monthly": [
                    {"month": period.isoformat(), "value": value}
                    for period, value in monthly
                ],
                "users": [
                    {
                        "username": username,
                        "total": round(sum(months.values()), 2),
                        "monthly": [
                            {"month": month, "value": round(value, 2)}
                            for month, value in sorted(months.items())
                        ],
                    }
                    for username, months in sorted(
                        users.items(), key=lambda item: -sum(item[1].values())
                    )
                ],
            }
        )
    return {"components": components, "as_of": today.isoformat()}


def _daily_rate(monthly: list[tuple], today: datetime.date) -> float | None:
    """Average daily use over the current month and the one before it.

    Billing records are monthly, so this is the finest rate they allow; the
    current month counts only the days elapsed.
    """
    recent = [
        (period, value)
        for period, value in monthly
        if period >= (today.replace(day=1) - relativedelta(months=1))
    ]
    if not recent:
        return None
    days = 0
    for period, _ in recent:
        if period == today.replace(day=1):
            days += today.day
        else:
            days += (period + relativedelta(months=1) - period).days
    total = sum(value for _, value in recent)
    return total / days if days and total else None
