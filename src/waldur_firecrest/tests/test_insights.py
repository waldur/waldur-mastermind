import datetime
import json

import responses
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status

from waldur_firecrest import charts, insights, outputs
from waldur_mastermind.chat.chart_spec import ChartSpecSerializer
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    ImpactLevel,
    LimitPeriods,
    MaintenanceState,
)
from waldur_mastermind.marketplace.tests import factories

from .test_workspace_api import (
    ACCOUNT,
    API_URL,
    SYSTEM,
    HpcWorkspaceTestBase,
    firecrest_job,
)

NOW = 1_760_000_000  # 2025-10-09
HOUR = 3600


def finished_job(
    job_id,
    name="wrf",
    state="COMPLETED",
    end=NOW - HOUR,
    elapsed=HOUR,
    limit=48 * HOUR,
    wait=600,
    cpus=128,
    cpu_efficiency=0.9,
    exit_code=0,
    partition="batch",
):
    start = end - elapsed
    return {
        "jobId": str(job_id),
        "name": name,
        "status": {"state": state, "exitCode": exit_code},
        "time": {
            "submission": start - wait,
            "start": start,
            "end": end,
            "elapsed": elapsed,
            "limit": limit,
            "totalCpu": int(cpus * elapsed * cpu_efficiency),
        },
        "account": ACCOUNT,
        "partition": partition,
        "allocationCpus": cpus,
        "requestedMemory": 1024,
        "maxRss": 512 * insights.MIB,
    }


def assert_valid_chart(test, chart):
    serializer = ChartSpecSerializer(data=json.loads(json.dumps(chart)))
    test.assertTrue(serializer.is_valid(), serializer.errors)


class JobMetricsTest(TestCase):
    def test_wait_efficiency_and_core_hours(self):
        metrics = insights.job_metrics(finished_job(1, cpu_efficiency=0.5))
        self.assertEqual(metrics["wait_seconds"], 600)
        self.assertEqual(metrics["cpu_hours"], 128)
        self.assertEqual(metrics["cpu_efficiency"], 0.5)
        self.assertEqual(metrics["memory_efficiency"], 0.5)
        self.assertEqual(metrics["max_rss_mb"], 512)

    def test_running_job_has_no_efficiency_yet(self):
        job = finished_job(1, state="RUNNING")
        self.assertIsNone(insights.job_metrics(job)["cpu_efficiency"])

    def test_missing_accounting_fields_give_none(self):
        metrics = insights.job_metrics(firecrest_job())
        self.assertIsNone(metrics["wait_seconds"])
        self.assertIsNone(metrics["cpu_efficiency"])
        self.assertIsNone(metrics["gpu_hours"])
        self.assertIsNone(metrics["energy_kwh"])

    def test_gpu_hours_and_energy(self):
        job = {
            **finished_job(1, elapsed=2 * HOUR),
            "allocationNodes": 2,
            "allocationGpus": 8,
            "consumedEnergy": 36_000_000,
        }
        metrics = insights.job_metrics(job)
        self.assertEqual(metrics["gpu_hours"], 16)
        self.assertEqual(metrics["energy_kwh"], 10)
        self.assertEqual(metrics["mean_power_w"], 5000)


class JobStatsTest(TestCase):
    def setUp(self):
        self.jobs = [
            *[finished_job(i, elapsed=2 * HOUR + i * 60) for i in range(1, 5)],
            finished_job(5, name="md", state="FAILED", exit_code=127),
            finished_job(6, name="md", state="FAILED", exit_code=127),
            finished_job(
                7, name="md", state="TIMEOUT", elapsed=4 * HOUR, limit=4 * HOUR
            ),
            finished_job(8, state="CANCELLED"),
            finished_job(9, end=NOW - 40 * 86400),  # outside the window
            finished_job(
                10, name="post", partition="debug", wait=30, cpu_efficiency=0.2
            ),
            finished_job(
                11, name="post", partition="debug", wait=60, cpu_efficiency=0.25
            ),
            finished_job(
                12, name="post", partition="debug", wait=90, cpu_efficiency=0.3
            ),
            {**firecrest_job("13", state="PENDING")},
        ]
        self.stats = insights.job_stats(self.jobs, days=14, now=NOW)

    def test_totals_count_window_only(self):
        totals = self.stats["totals"]
        self.assertEqual(totals["completed"], 7)
        self.assertEqual(totals["failed"], 2)
        self.assertEqual(totals["timeout"], 1)
        self.assertEqual(totals["cancelled"], 1)
        self.assertEqual(totals["pending"], 1)
        self.assertEqual(len(self.stats["outcomes_per_day"]), 15)

    def test_failures_grouped_by_exit_code(self):
        self.assertEqual(
            self.stats["failures"][0],
            {"exit_code": 127, "count": 2, "names": ["md"], "latest_job_id": "5"},
        )

    def test_wait_by_partition(self):
        debug = next(
            row
            for row in self.stats["wait_by_partition"]
            if row["partition"] == "debug"
        )
        self.assertEqual(debug["median_wait_seconds"], 60)
        self.assertEqual(debug["p90_wait_seconds"], 90)

    def test_suggests_shorter_limit_and_fewer_cores(self):
        kinds = {(s["kind"], s["name"]) for s in self.stats["suggestions"]}
        self.assertIn(("time_limit", "wrf"), kinds)
        self.assertIn(("cpu_efficiency", "post"), kinds)
        wrf = next(s for s in self.stats["suggestions"] if s["name"] == "wrf")
        self.assertEqual(wrf["current_limit_minutes"], 48 * 60)
        # Longest run 2h04m + 25% = 155 min; the next step is 4 h.
        self.assertEqual(wrf["suggested_limit_minutes"], 240)

    def test_charts_are_valid_specs(self):
        for build in (
            charts.outcomes,
            charts.walltime_fit,
            charts.efficiency,
            charts.queue_wait,
            charts.job_timeline,
        ):
            assert_valid_chart(self, build(self.stats))


NODES = [
    {
        "name": f"node{i:03d}",
        "partitions": ["batch"],
        "state": ["ALLOCATED"] if i < 3 else ["IDLE"],
        "cpus": 128,
        "cpuLoad": 128.0 if i < 3 else 0.1,
        "allocCpus": 128 if i < 3 else 0,
    }
    for i in range(1, 7)
] + [
    {
        "name": "node007",
        "partitions": ["batch"],
        "state": ["IDLE", "DRAIN"],
        "cpus": 128,
        "reason": "Bad DIMM",
    },
    {"name": "gpu001", "partitions": ["gpu"], "state": ["MIXED"], "cpus": 72},
]
RESERVATIONS = [
    {
        "name": "maint",
        "nodeList": "ALL",
        "startTime": NOW + 2 * HOUR,
        "endTime": NOW + 6 * HOUR,
        "maintenance": True,
    },
    {
        "name": "course",
        "nodeList": "gpu001",
        "startTime": NOW + 86400,
        "endTime": NOW + 2 * 86400,
        "maintenance": False,
    },
    {
        "name": "past",
        "nodeList": "node001",
        "startTime": NOW - 2 * 86400,
        "endTime": NOW - 86400,
    },
]


class ClusterLoadTest(TestCase):
    def setUp(self):
        self.load = insights.cluster_load(NODES, RESERVATIONS, now=NOW)

    def test_partition_summary(self):
        batch = next(p for p in self.load["partitions"] if p["name"] == "batch")
        self.assertEqual(batch["total"], 7)
        self.assertEqual(batch["allocated"], 2)
        self.assertEqual(batch["idle"], 4)
        self.assertEqual(batch["unavailable"], 1)

    def test_drain_wins_over_idle_and_keeps_reason(self):
        node = next(n for n in self.load["nodes"] if n["name"] == "node007")
        self.assertEqual(node["state"], "DRAIN")
        self.assertEqual(node["reason"], "Bad DIMM")

    def test_load_is_share_of_cpus(self):
        node = next(n for n in self.load["nodes"] if n["name"] == "node001")
        self.assertEqual(node["load_percent"], 100.0)

    def test_maintenance_comes_from_firecrest(self):
        maint, course = self.load["reservations"]
        self.assertTrue(maint["maintenance"])
        self.assertFalse(course["maintenance"])

    def test_reservations_skip_past_and_map_partitions(self):
        names = [r["name"] for r in self.load["reservations"]]
        self.assertEqual(names, ["maint", "course"])
        self.assertEqual(self.load["reservations"][0]["partitions"], ["batch", "gpu"])
        self.assertEqual(self.load["reservations"][1]["partitions"], ["gpu"])

    def test_long_job_warned_about_maintenance(self):
        reservations = self.load["reservations"]
        self.assertTrue(
            insights.maintenance_conflicts(reservations, "batch", 240, now=NOW)
        )
        self.assertFalse(
            insights.maintenance_conflicts(reservations, "batch", 60, now=NOW)
        )

    def test_charts_are_valid_specs(self):
        assert_valid_chart(self, charts.node_map(self.load))
        assert_valid_chart(self, charts.node_map(self.load, "gpu"))
        assert_valid_chart(self, charts.reservations(self.load))

    def test_hostlist_expansion(self):
        self.assertEqual(
            insights.expand_hostlist("node[001-003,007],gpu01"),
            {"node001", "node002", "node003", "node007", "gpu01"},
        )


@freeze_time("2026-10-17")
class AllocationUsageTest(TestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.cpu = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
            measured_unit="CPU-h",
            billing_type="usage",
            limit_period=LimitPeriods.TOTAL,
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            limits={"cpu": 500_000},
            end_date=datetime.date(2026, 12, 31),
        )
        self.offering_user = factories.OfferingUserFactory(
            offering=self.offering, username="alice"
        )
        for month in range(1, 11):
            usage = marketplace_models.ComponentUsage.objects.create(
                resource=self.resource,
                component=self.cpu,
                usage=30_000,
                date=timezone.now(),
                billing_period=datetime.date(2026, month, 1),
            )
            marketplace_models.ComponentUserUsage.objects.create(
                component_usage=usage,
                username="alice",
                user=self.offering_user,
                usage=20_000,
            )

    def test_consumption_forecast_and_users(self):
        usage = insights.allocation_usage(self.resource)
        component = usage["components"][0]
        self.assertEqual(component["consumed"], 300_000)
        self.assertEqual(component["percent"], 60.0)
        self.assertEqual(component["period_start"], "2026-01-01")
        self.assertEqual(component["period_end"], "2026-12-31")
        # September (30 days) + 17 days of October: 60000 / 47 per day.
        self.assertAlmostEqual(component["daily_rate"], 1276.6, places=1)
        self.assertEqual(component["runs_out"], "2027-03-23")
        self.assertFalse(component["runs_out_before_period_end"])
        self.assertEqual(component["users"][0]["username"], "alice")
        self.assertEqual(component["users"][0]["total"], 200_000)
        self.assertEqual(
            component["cumulative"][-1], {"date": "2026-10-17", "value": 300_000}
        )

    def test_charts_are_valid_specs(self):
        component = insights.allocation_usage(self.resource)["components"][0]
        assert_valid_chart(self, charts.allocation_burn(component, link="/x/"))
        assert_valid_chart(self, charts.usage_by_user(component))


GMX_XVG = """# This file was created by gmx energy
@    title "GROMACS Energies"
@    xaxis  label "Time (ps)"
@    yaxis  label "(kJ/mol), (K)"
@ s0 legend "Potential"
@ s1 legend "Temperature"
    0.000000  -258500.0  318.0
   10.000000  -259100.0  305.2
   20.000000  -259300.0  300.4
"""

RSL = (
    "\n".join(
        f"Timing for main: time 2026-10-18_{hour:02d}:00:00 on domain   1:    "
        f"{seconds:.5f} elapsed seconds"
        for hour, seconds in ((1, 30.0), (2, 30.0), (3, 30.0))
    )
    + "\nTiming for main: time 2026-10-18_03:00:00 on domain   2:    5.0 elapsed seconds\n"
)


class OutputSeriesTest(TestCase):
    def test_xvg_legends_and_axes(self):
        data = outputs.parse_xvg(GMX_XVG)
        self.assertEqual(data.title, "GROMACS Energies")
        self.assertEqual(data.x_label, "Time (ps)")
        self.assertEqual([s["name"] for s in data.series], ["Potential", "Temperature"])
        self.assertEqual(data.series[1]["points"][-1], [20.0, 300.4])
        self.assertEqual(data.summary["Temperature"]["last"], 300.4)

    def test_csv_numeric_columns(self):
        data = outputs.parse_csv("step,loss,label\n1,0.9,a\n2,0.5,b\n", "train.csv")
        self.assertEqual([s["name"] for s in data.series], ["loss"])
        self.assertEqual(data.x_label, "step")

    def test_wrf_progress_and_eta(self):
        data = outputs.parse_wrf_rsl(RSL, forecast_hours=6)
        summary = data.summary
        self.assertEqual(summary["start"], "2026-10-18_00:00:00")
        self.assertEqual(summary["simulated_hours"], 3)
        self.assertEqual(summary["wall_hours"], 0.025)
        self.assertEqual(summary["percent"], 50.0)
        self.assertEqual(summary["eta_hours"], 0.03)
        self.assertEqual(data.series[0]["points"][0], [0.0, 0.0])

    def test_namelist_forecast_length(self):
        self.assertEqual(
            outputs.forecast_hours_from_namelist(
                "&time_control\n run_days = 3,\n run_hours = 0,\n/"
            ),
            72,
        )

    def test_unknown_file_type(self):
        with self.assertRaises(outputs.UnsupportedOutput):
            outputs.parse("/home/a/model.nc", "")


class InsightEndpointsTest(HpcWorkspaceTestBase):
    @responses.activate
    def test_job_stats_with_charts(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            json={"jobs": [finished_job(1, end=int(timezone.now().timestamp()))]},
            match=[
                responses.matchers.query_param_matcher(
                    {"account": ACCOUNT, "time_window": "7d"}
                )
            ],
        )
        response = self.client.get(self.url("job-stats"), {"days": 7})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["totals"]["completed"], 1)
        self.assertEqual(len(response.data["charts"]), 5)

    def test_job_stats_stay_within_firecrest_windows(self):
        # FirecREST's longest window is 7 days; more would be a month of jobs.
        response = self.client.get(self.url("job-stats"), {"days": 30})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    def test_cluster_load(self):
        responses.get(f"{API_URL}/status/{SYSTEM}/nodes", json={"nodes": NODES})
        responses.get(
            f"{API_URL}/status/{SYSTEM}/reservations", json={"reservations": []}
        )
        response = self.client.get(self.url("cluster-load"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["nodes"]), 8)
        self.assertEqual(response.data["charts"][0]["chart_type"], "heatmap")

    def test_usage_without_components_is_empty(self):
        response = self.client.get(self.url("usage"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["components"], [])

    @responses.activate
    def test_output_series_of_xvg(self):
        responses.get(
            f"{API_URL}/filesystem/{SYSTEM}/ops/view", json={"output": GMX_XVG}
        )
        response = self.client.get(
            self.url("output-series"), {"path": "/home/hpc_9000/md/energy.xvg"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["kind"], "xvg")
        self.assertEqual(len(response.data["chart"]["series"]), 2)
        # Kelvin next to kJ/mol: temperature moves to the right-hand axis.
        self.assertEqual(response.data["chart"]["series"][1]["y_axis"], "secondary")
        self.assertEqual(response.data["chart"]["y2"]["label"], "Temperature")

    def test_output_series_rejects_unknown_type(self):
        response = self.client.get(self.url("output-series"), {"path": "/home/x/a.nc"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    def test_submit_sends_resource_request_and_warns_about_maintenance(self):
        now = timezone.now().timestamp()
        responses.get(f"{API_URL}/status/{SYSTEM}/nodes", json={"nodes": NODES})
        responses.get(
            f"{API_URL}/status/{SYSTEM}/reservations",
            json={
                "reservations": [
                    {
                        **RESERVATIONS[0],
                        "startTime": now + HOUR,
                        "endTime": now + 2 * HOUR,
                    }
                ]
            },
        )
        submit = responses.post(
            f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobId": 42}, status=201
        )
        response = self.client.post(
            self.url("jobs"),
            {
                "name": "wrf",
                "commands": "srun wrf.exe",
                "partition": "batch",
                "nodes": 4,
                "ntasks_per_node": 32,
                "cpus_per_task": 4,
                "memory_mb": 1000,
                "time_limit_minutes": 600,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            any("Maintenance 'maint'" in w for w in response.data["warnings"]),
            response.data["warnings"],
        )
        job = json.loads(submit.calls[0].request.body)["job"]
        self.assertEqual(job["nodes"], 4)
        self.assertNotIn("tasks", job)
        self.assertEqual(job["tasksPerNode"], 32)
        self.assertEqual(job["cpusPerTask"], 4)
        self.assertEqual(job["timeLimit"], 600)
        self.assertEqual(job["memoryPerNode"], 1000)


def announcement(name, start, end, outage=True, partitions=(), reserve=True):
    return {
        "uuid": "a" * 32,
        "name": name,
        "message": "Cooling upgrade",
        "state": "Scheduled",
        "impact": "Full outage" if outage else "Degraded performance",
        "outage": outage,
        "impact_description": "",
        "start": start,
        "end": end,
        "partitions": list(partitions),
        "reserve": reserve,
        "expected_reservation": f"waldur-maint-{name}" if reserve else "",
    }


class AnnouncedMaintenanceTest(TestCase):
    def test_held_announcement_and_unannounced_reservation(self):
        load = insights.cluster_load(
            NODES,
            RESERVATIONS,
            [announcement("held", NOW + 3 * HOUR, NOW + 5 * HOUR)],
            now=NOW,
        )
        [held] = load["announcements"]
        # The site's 'maint' reservation (ALL, +2h..+6h) covers it.
        self.assertTrue(held["reservation_in_place"])
        self.assertEqual(held["partitions"], ["batch", "gpu"])
        maint = next(r for r in load["reservations"] if r["name"] == "maint")
        self.assertTrue(maint["announced"])
        course = next(r for r in load["reservations"] if r["name"] == "course")
        self.assertIsNone(course["announced"])

    def test_announced_outage_not_held_warns_on_submit(self):
        load = insights.cluster_load(
            NODES,
            [],
            [announcement("cooling", NOW + 3 * HOUR, NOW + 9 * HOUR)],
            now=NOW,
        )
        [cooling] = load["announcements"]
        self.assertFalse(cooling["reservation_in_place"])
        warnings = insights.maintenance_conflicts(
            [], "batch", 240, now=NOW, announcements=load["announcements"]
        )
        self.assertIn("does not hold it back yet", warnings[0])
        self.assertFalse(
            insights.maintenance_conflicts(
                [], "batch", 60, now=NOW, announcements=load["announcements"]
            )
        )
        assert_valid_chart(self, charts.reservations(load))

    def test_notice_needs_no_reservation(self):
        load = insights.cluster_load(
            NODES,
            [],
            [announcement("slow", NOW + HOUR, NOW + 2 * HOUR, False, reserve=False)],
            now=NOW,
        )
        self.assertIsNone(load["announcements"][0]["reservation_in_place"])


class WorkspaceAnnouncementsTest(HpcWorkspaceTestBase):
    @responses.activate
    def test_cluster_load_lists_published_maintenance_only(self):
        self.offering.shared = True
        self.offering.plugin_options = {
            **self.offering.plugin_options,
            "slurm_maintenance_sync_enabled": True,
        }
        self.offering.save()
        start = timezone.now() + datetime.timedelta(days=1)
        for state in (MaintenanceState.SCHEDULED, MaintenanceState.DRAFT):
            factories.MaintenanceAnnouncementOfferingFactory(
                offering=self.offering,
                impact_level=ImpactLevel.PARTIAL_OUTAGE,
                affected_partitions=["gpu"],
                maintenance=factories.MaintenanceAnnouncementFactory(
                    state=state,
                    scheduled_start=start,
                    scheduled_end=start + datetime.timedelta(hours=4),
                ),
            )
        responses.get(f"{API_URL}/status/{SYSTEM}/nodes", json={"nodes": NODES})
        responses.get(
            f"{API_URL}/status/{SYSTEM}/reservations", json={"reservations": []}
        )
        response = self.client.get(self.url("cluster-load"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [published] = response.data["announcements"]
        self.assertEqual(published["partitions"], ["gpu"])
        self.assertTrue(published["expected_reservation"].startswith("waldur-maint-"))
        self.assertFalse(published["reservation_in_place"])
