import datetime
import io

import responses
from django.test import SimpleTestCase
from django.urls import reverse
from pypdf import PdfReader
from rest_framework import status

from waldur_core.configuration.testing import override_config
from waldur_firecrest import monitoring, pdf
from waldur_mastermind.chat.chart_spec import ChartSpecSerializer

from .test_workspace_api import API_URL, SYSTEM, HpcWorkspaceTestBase, firecrest_job

MONITOR_URL = "http://slurm-monitor:12000/api/v2"
CLUSTER = "hpc-demo"
START = 1_760_000_000
STEP = 300


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC).isoformat()


def gpu_job():
    return {
        **firecrest_job(job_id="86", state="COMPLETED"),
        "nodes": "gpu[01-02]",
        "allocationNodes": 2,
        "time": {"start": START, "end": START + 4 * STEP, "elapsed": 4 * STEP},
    }


def process_timeseries(nodes=("gpu01", "gpu02")):
    def node(index):
        return {
            "cpu_memory": [
                {
                    "time": iso(START + i * STEP),
                    "cpu_util": 6400.0,  # 64 cores' worth
                    "memory_resident": 64 * monitoring.KIB_PER_GIB,
                }
                for i in range(4)
            ],
            "gpus": {f"GPU-{index}-0": [], f"GPU-{index}-1": []},
        }

    return [{"job": 86, "epoch": 0, "nodes": {n: node(i) for i, n in enumerate(nodes)}}]


def card_series(node_index, utils):
    return [
        {
            "uuid": f"GPU-{node_index}-{card}",
            "index": card,
            "data": [
                {
                    "time": iso(START + i * STEP),
                    "ce_util": util,
                    "memory": 48 * monitoring.KIB_PER_GIB,
                    "memory_util": 50.0,
                    "power": 300.0,
                }
                for i, util in enumerate(utils)
            ],
        }
        # The node's other cards belong to other jobs.
        for card in (0, 1, 2)
    ]


@override_config(SLURM_MONITOR_ALLOWED_API_URLS=MONITOR_URL)
class JobPerformanceTest(HpcWorkspaceTestBase):
    def setUp(self):
        super().setUp()
        self.offering.plugin_options.update(
            slurm_monitor_api_url=MONITOR_URL, slurm_monitor_cluster_name=CLUSTER
        )
        self.offering.save()
        self.monitor = f"{MONITOR_URL}/cluster/{CLUSTER}"

    def mock_firecrest(self, job):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/{job['jobId']}", json={"jobs": [job]}
        )

    def performance(self, job_id="86"):
        return self.client.get(self.url("job-performance", job_id=job_id))

    @responses.activate
    def test_report_from_samples(self):
        self.mock_firecrest(gpu_job())
        series = responses.get(
            f"{self.monitor}/jobs/86/process/timeseries", json=process_timeseries()
        )
        for index, node in enumerate(("gpu01", "gpu02")):
            responses.get(
                f"{self.monitor}/nodes/{node}/gpu/timeseries",
                json={node: card_series(index, [0, 2, 80, 90])},
            )
        responses.get(
            f"{self.monitor}/jobs/86/process/tree",
            json={
                "job": 86,
                "epoch": 0,
                "nodes": {
                    "gpu01": {
                        "processes": {
                            "10": {
                                "ppid": 1,
                                "user": "u",
                                "cmd": "gmx_mpi",
                                "data": [
                                    {"cpu_util": 6000, "memory_resident": 1024},
                                    {"cpu_util": 6200, "memory_resident": 2048},
                                ],
                            },
                            "9": {
                                "ppid": 1,
                                "user": "u",
                                "cmd": "slurm_script",
                                "data": [{"cpu_util": 0, "memory_resident": 10}],
                            },
                        }
                    }
                },
            },
        )
        responses.get(
            f"{self.monitor}/jobs/86/report",
            json={"warnings": ["CPU allocation excessive"]},
        )

        response = self.performance()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.data
        self.assertTrue(data["available"])
        self.assertEqual(
            series.calls[0].request.headers["Authorization"], f"Bearer {self.token}"
        )
        self.assertIn("resolution_in_s=60", series.calls[0].request.url)
        summary = data["summary"]
        self.assertEqual(summary["nodes"], 2)
        self.assertEqual(summary["gpus"], 4)  # only the cards the job used
        self.assertEqual(summary["gpu_use_percent"], 43.0)
        self.assertEqual(summary["idle_share_percent"], 50.0)
        self.assertEqual(summary["gpu_memory_total_gib"], 96.0)
        self.assertEqual(summary["gpu_power_watts"], 300)
        self.assertEqual(summary["cpu_cores_mean"], 128.0)
        self.assertEqual(summary["host_memory_peak_gib"], 128.0)
        self.assertEqual(summary["coverage_percent"], 100.0)
        titles = [c["title"] for c in data["charts"]]
        self.assertEqual(
            titles,
            [
                "GPU use",
                "GPU memory used",
                "GPU power",
                "CPU cores used",
                "Host memory",
            ],
        )
        for spec in data["charts"]:
            self.assertTrue(ChartSpecSerializer(data=spec).is_valid())
        self.assertEqual(len(data["charts"][0]["series"]), 5)  # 4 GPUs + mean
        self.assertEqual(data["processes"][0]["command"], "gmx_mpi")
        self.assertEqual(data["processes"][0]["mean_cpu_percent"], 6100.0)
        self.assertEqual(data["warnings"], ["CPU allocation excessive"])

    def mock_samples(self):
        responses.get(
            f"{self.monitor}/jobs/86/process/timeseries", json=process_timeseries()
        )
        for index, node in enumerate(("gpu01", "gpu02")):
            responses.get(
                f"{self.monitor}/nodes/{node}/gpu/timeseries",
                json={node: card_series(index, [0, 2, 80, 90])},
            )
        responses.get(f"{self.monitor}/jobs/86/process/tree", json={"nodes": {}})
        responses.get(f"{self.monitor}/jobs/86/report", json={"warnings": []})

    @responses.activate
    def test_pdf_report(self):
        self.mock_firecrest(gpu_job())
        self.mock_samples()
        response = self.client.get(self.url("job-performance-pdf", job_id="86"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("job-86-report.pdf", response["Content-Disposition"])
        text = "".join(
            page.extract_text()
            for page in PdfReader(io.BytesIO(response.content)).pages
        )
        for expected in ("Job 86", "GPU use", "IDLE SHARE", "Host memory", "Partition"):
            self.assertIn(expected, text)

    @responses.activate
    def test_pdf_report_without_monitor_keeps_accounting(self):
        self.mock_firecrest(gpu_job())
        responses.get(f"{self.monitor}/jobs/86/process/timeseries", status=502)
        response = self.client.get(self.url("job-performance-pdf", job_id="86"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
        self.assertIn("cluster monitor returned 502", text)
        self.assertIn("Partition", text)

    @responses.activate
    def test_job_of_another_allocation_is_not_asked_for(self):
        self.mock_firecrest({**gpu_job(), "account": "someone_else"})
        response = self.performance()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(
            any(MONITOR_URL in call.request.url for call in responses.calls)
        )

    @responses.activate
    def test_pending_job_has_nothing_yet(self):
        job = {**gpu_job(), "status": {"state": "PENDING"}, "time": {"start": None}}
        self.mock_firecrest(job)
        response = self.performance()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["available"])

    @responses.activate
    def test_monitor_refusing_the_token_is_forbidden(self):
        self.mock_firecrest(gpu_job())
        responses.get(
            f"{self.monitor}/jobs/86/process/timeseries",
            status=401,
            json={"detail": "Could not validate credentials"},
        )
        response = self.performance()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(SLURM_MONITOR_ALLOWED_API_URLS="")
    @responses.activate
    def test_monitor_outside_allowlist(self):
        self.mock_firecrest(gpu_job())
        response = self.client.get(self.url())
        self.assertFalse(response.data["monitoring_enabled"])
        response = self.performance()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReportWithoutMonitorTest(HpcWorkspaceTestBase):
    """Only FirecREST connected: the workspace, and its PDF, still work."""

    @responses.activate
    def test_pdf_from_accounting_alone(self):
        job = {
            **gpu_job(),
            "allocationCpus": 128,
            "allocationGpus": 8,
            "consumedEnergy": 36_000_000,
        }
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs/86", json={"jobs": [job]})
        self.assertFalse(
            self.client.get(reverse("hpc-workspace-list")).data[0]["monitoring_enabled"]
        )

        response = self.client.get(self.url("job-performance-pdf", job_id="86"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
        for expected in ("CPU EFFICIENCY", "GPUS", "ENERGY", "10.0 kWh"):
            self.assertIn(expected, text)
        self.assertNotIn("monitor", text.lower())
        # Nothing but FirecREST was asked.
        self.assertTrue(all(c.request.url.startswith(API_URL) for c in responses.calls))


class JobWindowTest(SimpleTestCase):
    def test_running_job_reaches_now_and_long_jobs_are_capped(self):
        now = START + 30 * 86400
        job = {
            "status": {"state": "RUNNING"},
            "time": {"start": START, "end": None},
        }
        window = monitoring.job_window(job, now=now)
        self.assertEqual(window["end_time_in_s"], now)
        self.assertLess(
            window["end_time_in_s"] - window["start_time_in_s"],
            monitoring.MAX_WINDOW_SECONDS,
        )
        self.assertGreaterEqual(window["resolution_in_s"], 60)


class PdfChartGeometryTest(SimpleTestCase):
    def test_axis_covers_buckets_before_the_job_start(self):
        # The monitor stamps samples with their bucket start: 60 s before here.
        start = START + 30
        spec = {
            "title": "CPU cores used",
            "x": {"type": "time"},
            "y": {"type": "value", "label": "cores"},
            "series": [
                {
                    "name": "All nodes",
                    "tone": "info",
                    "points": [[(START + i * 60) * 1000, 4] for i in range(6)],
                }
            ],
        }
        geometry = pdf.chart_geometry(spec, start, START + 300)
        points = geometry["lines"][0]["segments"][0]
        self.assertEqual(points[0][0], 0)
        self.assertTrue(all(0 <= x <= 1 for x, _ in points))
        labels = [tick["label"] for tick in geometry["x_ticks"]]
        # Round minutes, evenly spaced, inside the axis.
        self.assertEqual(len(set(labels)), len(labels))
        self.assertTrue(all(0 <= tick["pos"] <= 1 for tick in geometry["x_ticks"]))
