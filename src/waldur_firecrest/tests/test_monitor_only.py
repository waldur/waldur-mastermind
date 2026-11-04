"""An HPC workspace whose offering is connected to slurm-monitor only."""

import time

import jwt
import responses
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status, test

from waldur_auth_social import oauth_tokens
from waldur_auth_social.models import OAuthToken
from waldur_core.configuration.testing import override_config
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_firecrest import monitoring
from waldur_firecrest.client import FirecrestException
from waldur_firecrest.workspace import HpcWorkspace
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories

MONITOR_URL = "http://slurm-monitor:12000/api/v2"
CLUSTER = "cluster-a"
ACCOUNT = "alloc_1"
USERNAME = "hpc_9000"
MONITOR = f"{MONITOR_URL}/cluster/{CLUSTER}"


def make_token(username=USERNAME):
    return jwt.encode(
        {"exp": int(time.time()) + 3600, "preferred_username": username},
        "k" * 32,
        algorithm="HS256",
    )


def job_row(job_id=11, step="", account=ACCOUNT, user=USERNAME, **sacct):
    """A row of slurm-monitor's job list: one per job step."""
    return {
        "cluster": CLUSTER,
        "job_id": job_id,
        "job_step": step,
        "job_name": "hello",
        "job_state": "COMPLETED",
        "array_job_id": None,
        "array_task_id": None,
        "user_name": user,
        "account": account,
        "submit_time": "2026-11-03T10:00:00+00:00",
        "start_time": "2026-11-03T10:05:00+00:00",
        "end_time": "2026-11-03T11:05:00+00:00",
        # Minutes, as Sonar reports Slurm's TimelimitRaw.
        "time_limit": 120,
        "exit_code": 0,
        "partition": "batch",
        "nodes": ["node1", "node2"],
        "requested_cpus": 16,
        "requested_memory_per_node": 8 * 1024 * 1024,
        "requested_node_count": 2,
        "allocated_resources": "cpu=16,mem=16G,node=2",
        "used_gpu_uuids": [],
        "sacct": sacct or None,
    }


def accounting(**fields):
    return {
        "AllocTRES": "cpu=16,gres/gpu=2,mem=16G,node=2",
        "ElapsedRaw": 3600,
        "UserCPU": 28000,
        "SystemCPU": 800,
        **fields,
    }


@override_config(
    FIRECREST_ALLOWED_API_URLS="", SLURM_MONITOR_ALLOWED_API_URLS=MONITOR_URL
)
class MonitorOnlyTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.user = self.fixture.admin
        self.offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            plugin_options={
                "slurm_monitor_api_url": MONITOR_URL,
                "slurm_monitor_cluster_name": CLUSTER,
            },
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
            backend_id=ACCOUNT,
        )
        factories.OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username=USERNAME,
            state=OfferingUserStates.OK,
        )
        cache.clear()
        OAuthToken.objects.create(
            user=self.user, provider="keycloak", refresh_token="refresh-1"
        )
        oauth_tokens.cache_access_token(self.user, make_token())
        self.client.force_authenticate(self.user)

    def url(self, action=None, **kwargs):
        if action is None:
            return reverse(
                "hpc-workspace-detail", kwargs={"uuid": self.resource.uuid.hex}
            )
        return reverse(
            f"hpc-workspace-{action}",
            kwargs={"uuid": self.resource.uuid.hex, **kwargs},
        )

    def monitor_jobs(self, *rows):
        return responses.get(f"{MONITOR}/jobs", json={"jobs": list(rows)})


class WorkspaceTest(MonitorOnlyTestBase):
    @responses.activate
    def test_allocation_is_a_workspace_without_firecrest(self):
        response = self.client.get(reverse("hpc-workspace-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["resource_uuid"] for item in response.data],
            [str(self.resource.uuid)],
        )

    @responses.activate
    def test_overview_says_what_the_workspace_can_do(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["firecrest_enabled"])
        self.assertTrue(response.data["monitoring_enabled"])
        self.assertEqual(response.data["username"], USERNAME)
        self.assertIsNone(response.data["error"])
        # Nothing to ask FirecREST.
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_overview_tells_a_login_the_monitor_does_not_know(self):
        oauth_tokens.forget_access_tokens(self.user.pk)
        oauth_tokens.cache_access_token(self.user, make_token("someone_else"))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("knows you as 'someone_else'", response.data["error"])
        self.assertEqual(len(responses.calls), 0)

    @responses.activate
    def test_overview_says_when_there_is_no_sign_in_to_use(self):
        OAuthToken.objects.filter(user=self.user).delete()
        oauth_tokens.forget_access_tokens(self.user.pk)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["error"])
        self.assertEqual(len(responses.calls), 0)

    def test_list_narrows_to_one_resource(self):
        factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
            backend_id="alloc_2",
        )
        url = reverse("hpc-workspace-list")
        self.assertEqual(len(self.client.get(url).data), 2)
        response = self.client.get(url, {"resource_uuid": self.resource.uuid.hex})
        self.assertEqual(
            [item["resource_uuid"] for item in response.data],
            [str(self.resource.uuid)],
        )
        response = self.client.get(url, {"resource_uuid": "not-a-uuid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    def test_cluster_name_stays_one_path_segment(self):
        self.offering.plugin_options["slurm_monitor_cluster_name"] = "a/../b"
        self.offering.save()
        responses.get(f"{MONITOR_URL}/cluster/a%2F..%2Fb/jobs", json={"jobs": []})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            responses.calls[0].request.url.split("?")[0],
            f"{MONITOR_URL}/cluster/a%2F..%2Fb/jobs",
        )

    @responses.activate
    def test_running_cancelling_and_files_need_firecrest(self):
        for response in (
            self.client.post(
                self.url("jobs"),
                {"name": "x", "commands": "hostname", "time_limit_minutes": 5},
                format="json",
            ),
            self.client.post(self.url("cancel-job", job_id="11")),
            self.client.get(self.url("files")),
            self.client.get(self.url("cluster-load")),
        ):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("need FirecREST", str(response.data))
        self.assertEqual(len(responses.calls), 0)

    def test_partitions_are_the_published_ones(self):
        factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="batch", max_time=120
        )
        response = self.client.get(self.url("partitions"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([p["name"] for p in response.data], ["batch"])
        self.assertTrue(response.data[0]["published"])
        self.assertFalse(response.data[0]["live"])


class JobsTest(MonitorOnlyTestBase):
    @responses.activate
    def test_jobs_are_the_allocations_own_from_the_monitor(self):
        self.monitor_jobs(
            job_row(11, **accounting()),
            job_row(11, step="batch", MaxRSS=6 * 1024**3),
            job_row(11, step="0", MaxRSS=7 * 1024**3),
            job_row(12, account="other_alloc", **accounting()),
            # The monitor scopes jobs to the token's user; a colleague's job in
            # the same allocation still stays out.
            job_row(13, user="someone_else", **accounting()),
        )
        response = self.client.get(self.url("jobs"), {"time_window": "7d"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([job["job_id"] for job in response.data], ["11"])
        job = response.data[0]
        self.assertEqual(job["state"], "COMPLETED")
        self.assertEqual(job["allocation_cpus"], 16)
        self.assertEqual(job["allocation_nodes"], 2)
        self.assertEqual(job["requested_memory_mb"], 16 * 1024)
        self.assertEqual(job["elapsed"], 3600)
        self.assertEqual(job["time_limit"], 7200)
        self.assertEqual(job["wait_seconds"], 300)
        self.assertEqual(job["cpu_time_seconds"], 28800)
        self.assertEqual(job["cpu_efficiency"], 0.5)
        # The largest step peak, as FirecREST reports it.
        self.assertEqual(job["max_rss_mb"], 7 * 1024)
        self.assertEqual(job["allocation_gpus"], 2)
        params = responses.calls[0].request.params
        self.assertEqual(
            int(params["end_time_in_s"]) - int(params["start_time_in_s"]), 7 * 86400
        )

    @responses.activate
    def test_job_takes_its_accounting_from_the_job_list(self):
        responses.get(f"{MONITOR}/jobs/11", json=job_row(11))
        self.monitor_jobs(job_row(11, **accounting()), job_row(14, **accounting()))

        response = self.client.get(self.url("job", job_id="11"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["cpu_time_seconds"], 28800)
        # The list over the job's run, from its start to its end.
        params = responses.calls[1].request.params
        self.assertEqual(int(params["start_time_in_s"]), 1793700300)
        self.assertEqual(int(params["end_time_in_s"]), 1793703900)

    @responses.activate
    def test_job_of_another_allocation_is_not_found(self):
        responses.get(f"{MONITOR}/jobs/12", json=job_row(12, account="other_alloc"))
        self.monitor_jobs()
        response = self.client.get(self.url("job", job_id="12"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @responses.activate
    def test_job_the_monitor_does_not_have_is_not_found(self):
        responses.get(
            f"{MONITOR}/jobs/12", status=404, json={"detail": "Job 12 not found"}
        )
        response = self.client.get(self.url("job", job_id="12"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @responses.activate
    def test_monitor_failure_on_a_job_is_bad_gateway(self):
        responses.get(f"{MONITOR}/jobs/12", status=500, body="Internal Server Error")
        response = self.client.get(self.url("job", job_id="12"))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_job_id_in_other_digits_is_not_found(self):
        workspace = HpcWorkspace.for_resource(self.resource, self.user)
        with self.assertRaises(FirecrestException) as raised:
            workspace.get_job("\u0661\u0662")  # Arabic-Indic "12"
        self.assertEqual(raised.exception.status_code, 404)

    def test_job_id_that_is_not_a_slurm_monitor_id_is_not_found(self):
        # The assistant's tools pass a job id on as the user typed it.
        workspace = HpcWorkspace.for_resource(self.resource, self.user)
        with self.assertRaises(FirecrestException) as raised:
            workspace.get_job("12; rm")
        self.assertEqual(raised.exception.status_code, 404)

    @responses.activate
    def test_job_statistics_come_from_the_monitor(self):
        self.monitor_jobs(job_row(11, **accounting()))
        response = self.client.get(self.url("job-stats"), {"days": 7})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["totals"]["completed"], 1)

    @responses.activate
    def test_monitor_outage_is_bad_gateway(self):
        responses.get(f"{MONITOR}/jobs", status=503, json={"detail": "down"})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    @responses.activate
    def test_refused_token_is_forbidden(self):
        responses.get(f"{MONITOR}/jobs", status=401, json={"detail": "bad token"})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @responses.activate
    def test_monitor_login_must_be_the_allocations(self):
        oauth_tokens.forget_access_tokens(self.user.pk)
        oauth_tokens.cache_access_token(self.user, make_token("someone_else"))

        response = self.client.get(self.url("jobs"))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("knows you as 'someone_else'", str(response.data))
        self.assertEqual(len(responses.calls), 0)


class JobsFromRowsTest(test.APITestCase):
    def test_a_job_without_accounting_keeps_what_the_monitor_knows(self):
        (job,) = monitoring.jobs_from_rows([job_row(11)])
        self.assertEqual(job["jobId"], "11")
        self.assertEqual(job["allocationCpus"], 16)
        self.assertEqual(job["nodes"], "node1,node2")
        self.assertIsNone(job["time"]["elapsed"])
        self.assertIsNone(job["time"]["totalCpu"])
        self.assertIsNone(job["maxRss"])
        self.assertIsNone(job["allocationGpus"])
