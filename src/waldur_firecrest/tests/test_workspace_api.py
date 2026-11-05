import json
import time
from types import SimpleNamespace

import jwt
import responses
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_auth_social import oauth_tokens
from waldur_auth_social.models import IdentityProvider, OAuthToken
from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_firecrest.client import FirecrestClient
from waldur_firecrest.views import IsOwnSignIn
from waldur_firecrest.workspace import identity_cache_key
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories

API_URL = "http://firecrest:5000"
SYSTEM = "slurm-a"
ACCOUNT = "alloc_1"
USERNAME = "hpc_9000"


def make_token(expires_in=3600):
    return jwt.encode({"exp": int(time.time()) + expires_in}, "k", algorithm="HS256")


def firecrest_job(job_id="11", account=ACCOUNT, state="RUNNING"):
    return {
        "jobId": job_id,
        "name": "hello",
        "status": {"state": state, "stateReason": None, "exitCode": 0},
        "time": {"elapsed": 5, "start": 1700000000, "end": None, "limit": 60},
        "account": account,
        "allocationNodes": 1,
        "cluster": SYSTEM,
        "nodes": "node1",
        "partition": "batch",
        "user": USERNAME,
        "workingDirectory": f"/home/{USERNAME}",
    }


@override_config(FIRECREST_ALLOWED_API_URLS=API_URL)
class HpcWorkspaceTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.user = self.fixture.admin
        self.offering = factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            plugin_options={
                "firecrest_api_url": API_URL,
                "firecrest_system_name": SYSTEM,
            },
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
            backend_id=ACCOUNT,
        )
        self.offering_user = factories.OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username=USERNAME,
            state=OfferingUserStates.OK,
        )
        cache.clear()
        # FirecREST's view of the user agrees; IdentityTest covers the check.
        cache.set(identity_cache_key(self.user, self.offering, ACCOUNT), True)
        self.token = make_token()
        OAuthToken.objects.create(
            user=self.user, provider="keycloak", refresh_token="refresh-1"
        )
        oauth_tokens.cache_access_token(self.user, self.token)
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


class WorkspaceListTest(HpcWorkspaceTestBase):
    def test_member_sees_allocation(self):
        response = self.client.get(reverse("hpc-workspace-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["account"], ACCOUNT)
        self.assertEqual(response.data[0]["system_name"], SYSTEM)

    def test_outsider_sees_nothing(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(reverse("hpc-workspace-list"))
        self.assertEqual(len(response.data), 0)

    def test_member_without_resource_list_permission_sees_nothing(self):
        # Same rule as the consumer resource list: no workspace for a
        # resource the user cannot open.
        ProjectRole.ADMIN.delete_permission(PermissionEnum.LIST_RESOURCES)
        response = self.client.get(reverse("hpc-workspace-list"))
        self.assertEqual(len(response.data), 0)

    @override_config(FIRECREST_ALLOWED_API_URLS="https://other.example.com")
    def test_offering_url_outside_allowlist_is_hidden(self):
        response = self.client.get(reverse("hpc-workspace-list"))
        self.assertEqual(len(response.data), 0)
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_config(PAT_ENABLED=True)
class DelegatedCallerTest(HpcWorkspaceTestBase):
    """The workspace acts on the cluster as the user; only their own sign-in
    may use it."""

    def test_personal_access_token_is_refused(self):
        # No permission scopes the workspace down, so not even a broad token.
        use_pat(self.client, self.user, PermissionEnum.UPDATE_RESOURCE.value)
        self.assertEqual(
            self.client.get(reverse("hpc-workspace-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        response = self.client.post(
            self.url("jobs"),
            {"name": "x", "commands": "hostname", "time_limit_minutes": 5},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_impersonation_is_refused(self):
        request = SimpleNamespace(
            auth=None, user=SimpleNamespace(impersonator=self.fixture.staff)
        )
        self.assertFalse(IsOwnSignIn().has_permission(request, None))

    def test_identity_provider_token_is_refused(self):
        request = SimpleNamespace(auth={"sub": "alice"}, user=self.user)
        self.assertFalse(IsOwnSignIn().has_permission(request, None))

    def test_api_token_is_accepted(self):
        token, _ = Token.objects.get_or_create(user=self.user)
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.get(reverse("hpc-workspace-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class JobsTest(HpcWorkspaceTestBase):
    @responses.activate
    def test_list_is_newest_submission_first(self):
        # Imported history can carry higher ids than jobs submitted later.
        old = {**firecrest_job("90"), "time": {"submission": 1_000, "elapsed": 1}}
        new = {**firecrest_job("12"), "time": {"submission": 2_000, "elapsed": 1}}
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobs": [old, new]})
        response = self.client.get(self.url("jobs"))
        self.assertEqual([j["job_id"] for j in response.data], ["12", "90"])

    @responses.activate
    def test_list_is_scoped_to_allocation_account(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            json={"jobs": [firecrest_job()]},
            match=[responses.matchers.query_param_matcher({"account": ACCOUNT})],
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data[0]["job_id"], "11")
        self.assertEqual(response.data[0]["state"], "RUNNING")
        self.assertEqual(
            responses.calls[0].request.headers["Authorization"], f"Bearer {self.token}"
        )

    @responses.activate
    def test_list_drops_jobs_firecrest_returns_for_other_accounts(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            json={"jobs": [firecrest_job("11"), firecrest_job("12", account="other")]},
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual([job["job_id"] for job in response.data], ["11"])

    @responses.activate
    def test_list_is_newest_first_with_elapsed_from_start_and_end(self):
        # FirecREST returns slurmctld jobs with elapsed=None, start and end only.
        finished = firecrest_job("9", state="COMPLETED")
        finished["time"] = {"elapsed": None, "start": 1700000000, "end": 1700000042}
        queued = firecrest_job("10", state="PENDING")
        queued["time"] = {"elapsed": None, "start": 1700000100, "end": 1700003700}
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            json={"jobs": [finished, firecrest_job("11"), queued]},
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual(
            [(job["job_id"], job["elapsed"]) for job in response.data],
            [("11", 5), ("10", None), ("9", 42)],
        )

    @responses.activate
    def test_job_of_another_account_is_not_found(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/12",
            json={"jobs": [firecrest_job("12", account="other")]},
        )
        response = self.client.get(self.url("job", job_id="12"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @responses.activate
    def test_cancel_job_of_another_account_is_refused(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/12",
            json={"jobs": [firecrest_job("12", account="other")]},
        )
        cancel = responses.delete(f"{API_URL}/compute/{SYSTEM}/jobs/12")
        response = self.client.post(self.url("cancel-job", job_id="12"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(cancel.call_count, 0)

    @responses.activate
    def test_submit_charges_allocation_account(self):
        submit = responses.post(
            f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobId": 42}, status=201
        )
        response = self.client.post(
            self.url("jobs"),
            {
                "name": "hello",
                "commands": "srun hostname",
                "modules": ["module load GROMACS/2024.1-foss-2023b"],
                "time_limit_minutes": 30,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["job_id"], "42")
        job = json.loads(submit.calls[0].request.body)["job"]
        self.assertEqual(job["account"], ACCOUNT)
        self.assertEqual(job["workingDirectory"], f"/home/{USERNAME}")
        self.assertIn(f"#SBATCH --account={ACCOUNT}", job["script"])
        self.assertIn("#SBATCH --time=00:30:00", job["script"])
        self.assertIn("module load GROMACS/2024.1-foss-2023b", job["script"])

    @responses.activate
    def test_submit_writes_the_job_files(self):
        submit = responses.post(
            f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobId": 43}, status=201
        )
        response = self.client.post(
            self.url("jobs"),
            {
                "name": "prog",
                "commands": "python3 prog.py",
                "files": [{"name": "prog.py", "content": "print(1)\n"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        job = json.loads(submit.calls[0].request.body)["job"]
        self.assertIn(
            "cat > prog.py <<'EOF'\nprint(1)\nEOF\n\npython3 prog.py\n", job["script"]
        )

    @responses.activate
    def test_submit_beyond_partition_limit_is_refused_before_firecrest(self):
        factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="batch", max_time=60, max_nodes=2
        )
        submit = responses.post(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobId": 1})
        response = self.client.post(
            self.url("jobs"),
            {
                "name": "big",
                "commands": "srun a.out",
                "partition": "batch",
                "nodes": 4,
                "time_limit_minutes": 120,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(submit.call_count, 0)

    def test_setup_lines_must_be_module_commands(self):
        response = self.client.post(
            self.url("jobs"),
            {"name": "x", "commands": "true", "modules": ["rm -rf ~"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_preview_reports_errors_without_submitting(self):
        factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="batch", max_time=60
        )
        response = self.client.post(
            self.url("preview-script"),
            {"name": "x", "commands": "true", "partition": "gpu"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("#SBATCH --partition=gpu", response.data["script"])
        self.assertTrue(response.data["errors"])

    def test_preview_writes_the_job_files_before_the_commands(self):
        program = 'print("EOF")\nEOF\nx = "$HOME"\n'
        response = self.client.post(
            self.url("preview-script"),
            {
                "name": "x",
                "commands": "python3 prog.py",
                "files": [{"name": "prog.py", "content": program}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        script = response.data["script"]
        # A line "EOF" in the file must not end the here-document early, and
        # the quoted delimiter keeps $HOME as written.
        self.assertIn(
            "cat > prog.py <<'_EOF_'\n"
            'print("EOF")\nEOF\nx = "$HOME"\n_EOF_\n\npython3 prog.py\n',
            script,
        )

    def test_job_files_are_plain_visible_names_written_once(self):
        for files in (
            [{"name": "../prog.py", "content": "x"}],
            [{"name": "dir/prog.py", "content": "x"}],
            [{"name": "..", "content": "x"}],
            [{"name": ".bashrc", "content": "x"}],
            [{"name": "a.py", "content": "1"}, {"name": "a.py", "content": "2"}],
            [{"name": f"f{i}.py", "content": "x"} for i in range(17)],
            [{"name": f"f{i}.py", "content": "x" * 60000} for i in range(5)],
        ):
            response = self.client.post(
                self.url("preview-script"),
                {"name": "x", "commands": "true", "files": files},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, files)

    def test_files_are_written_in_order_each_ending_with_one_newline(self):
        response = self.client.post(
            self.url("preview-script"),
            {
                "name": "x",
                "commands": "python3 a.py b.txt",
                "modules": ["module load Python/3.11.5-GCCcore-13.2.0"],
                "files": [
                    {"name": "a.py", "content": "print(1)"},
                    {"name": "b.txt", "content": "x\r\ny\n\n"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            response.data["script"].endswith(
                "module load Python/3.11.5-GCCcore-13.2.0\n\n"
                "cat > a.py <<'EOF'\nprint(1)\nEOF\n\n"
                "cat > b.txt <<'EOF'\nx\r\ny\nEOF\n\n"
                "python3 a.py b.txt\n"
            ),
            response.data["script"],
        )

    @responses.activate
    def test_job_output_tails_stdout(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/11", json={"jobs": [firecrest_job()]}
        )
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs/11/metadata",
            json={
                "jobs": [
                    {
                        "jobId": "11",
                        "script": "#!/bin/bash\nsrun hostname\n",
                        "standardOutput": "hello-11.out",
                        "standardError": f"/home/{USERNAME}/hello-11.err",
                    }
                ]
            },
        )
        responses.get(
            f"{API_URL}/filesystem/{SYSTEM}/ops/tail",
            json={
                "output": {
                    "content": "node1\n",
                    "contentType": "lines",
                    "startPosition": 0,
                    "endPosition": 1,
                }
            },
        )
        response = self.client.get(self.url("job-output", job_id="11"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["standard_output"]["content"], "node1\n")
        self.assertEqual(
            response.data["standard_output"]["path"], f"/home/{USERNAME}/hello-11.out"
        )

    @responses.activate
    def test_firecrest_outage_is_bad_gateway(self):
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", status=503, json={})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)


class TokenTest(HpcWorkspaceTestBase):
    def setUp(self):
        super().setUp()
        IdentityProvider.objects.create(
            provider="keycloak",
            client_id="waldur",
            client_secret="secret",
            token_url="http://keycloak:8080/realms/e2e/protocol/openid-connect/token",
            label="Keycloak",
        )

    @responses.activate
    def test_expired_token_is_refreshed_and_rotated(self):
        oauth_tokens.forget_access_tokens(self.user.pk)
        fresh = make_token()
        responses.post(
            "http://keycloak:8080/realms/e2e/protocol/openid-connect/token",
            json={"access_token": fresh, "refresh_token": "refresh-2"},
        )
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobs": []})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            responses.calls[1].request.headers["Authorization"], f"Bearer {fresh}"
        )
        stored = OAuthToken.objects.get(user=self.user)
        self.assertEqual(stored.refresh_token, "refresh-2")

    @responses.activate
    def test_token_is_exchanged_for_an_audience_allowlisted_endpoint(self):
        exchanged = make_token()
        responses.post(
            "http://keycloak:8080/realms/e2e/protocol/openid-connect/token",
            json={"access_token": exchanged},
        )
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobs": []})
        with override_config(FIRECREST_ALLOWED_API_URLS=f"{API_URL}|firecrest"):
            response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("audience=firecrest", responses.calls[0].request.body)
        self.assertIn(f"subject_token={self.token}", responses.calls[0].request.body)
        # FirecREST gets the exchanged token, never the sign-in token.
        self.assertEqual(
            responses.calls[1].request.headers["Authorization"], f"Bearer {exchanged}"
        )

    @responses.activate
    def test_failed_refresh_asks_to_sign_in_again(self):
        oauth_tokens.forget_access_tokens(self.user.pk)
        responses.post(
            "http://keycloak:8080/realms/e2e/protocol/openid-connect/token",
            status=400,
            json={"error": "invalid_grant"},
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Sign in again", response.data["detail"])
        self.assertEqual(response.data["code"], "identity_provider_sign_in_required")
        self.assertEqual(response.data["identity_provider"], "keycloak")
        # A refresh token the identity provider refused is useless: drop it.
        self.assertFalse(OAuthToken.objects.filter(user=self.user).exists())

    @responses.activate
    def test_overview_names_the_provider_to_sign_in_again_with(self):
        oauth_tokens.forget_access_tokens(self.user.pk)
        responses.post(
            "http://keycloak:8080/realms/e2e/protocol/openid-connect/token",
            status=400,
            json={"error": "invalid_grant"},
        )
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Sign in again", response.data["error"])
        self.assertEqual(response.data["sign_in_provider"], "keycloak")

    def test_user_without_oidc_login_gets_explanation(self):
        OAuthToken.objects.filter(user=self.user).delete()
        oauth_tokens.forget_access_tokens(self.user.pk)
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("identity provider", str(response.data))


class OverviewAndPartitionsTest(HpcWorkspaceTestBase):
    @responses.activate
    def test_overview_reports_health_and_identity(self):
        responses.get(
            f"{API_URL}/status/systems",
            json={
                "systems": [
                    {
                        "name": SYSTEM,
                        "servicesHealth": [
                            {"serviceType": "scheduler", "healthy": True},
                            {"serviceType": "ssh", "healthy": True},
                        ],
                    }
                ]
            },
        )
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["username"], USERNAME)
        self.assertTrue(response.data["healthy"])
        self.assertIsNone(response.data["error"])

    @responses.activate
    def test_overview_reports_firecrest_outage_inline(self):
        responses.get(f"{API_URL}/status/systems", status=500, json={})
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("500", response.data["error"])

    @responses.activate
    def test_partitions_merge_published_limits_with_live_nodes(self):
        partition = factories.OfferingPartitionFactory(
            offering=self.offering,
            partition_name="batch",
            max_time=1440,
            cpu_arch="x86_64/amd/zen3",
        )
        qos = marketplace_models.SlurmOfferingQoS.objects.create(
            offering=self.offering, name="normal"
        )
        marketplace_models.SlurmPartitionQoS.objects.create(
            partition=partition, qos=qos, is_default=True
        )
        responses.get(
            f"{API_URL}/status/{SYSTEM}/partitions",
            json={
                "partitions": [
                    {"name": "batch", "cpus": 256, "totalNodes": 2, "partition": "UP"}
                ]
            },
        )
        responses.get(
            f"{API_URL}/status/{SYSTEM}/nodes",
            json={
                "nodes": [
                    {
                        "name": "n1",
                        "cpus": 128,
                        "state": ["IDLE"],
                        "partitions": ["batch"],
                    },
                    {
                        "name": "n2",
                        "cpus": 128,
                        "state": ["ALLOCATED"],
                        "partitions": ["batch"],
                    },
                ]
            },
        )
        response = self.client.get(self.url("partitions"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        batch = response.data[0]
        self.assertEqual(batch["idle_nodes"], 1)
        self.assertEqual(batch["total_nodes"], 2)
        self.assertEqual(batch["max_time"], 1440)
        self.assertEqual(batch["default_qos"], "normal")
        self.assertTrue(batch["published"] and batch["live"])


class FirecrestErrorsTest(HpcWorkspaceTestBase):
    @responses.activate
    def test_rejected_token_asks_to_sign_in_again(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            status=401,
            json={"message": "Access token is invalid."},
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "identity_provider_sign_in_required")
        self.assertEqual(response.data["identity_provider"], "keycloak")
        self.assertIn("identity provider token", response.data["detail"])

    @responses.activate
    def test_overview_offers_sign_in_when_token_is_rejected(self):
        responses.get(f"{API_URL}/status/systems", status=401, json={})
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sign_in_provider"], "keycloak")

    @responses.activate
    def test_failed_ssh_login_explains_missing_cluster_access(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            status=424,
            json={"message": "Unable to establish SSH connection."},
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("SSH access to the login node", response.data["detail"])
        self.assertIn("Unable to establish SSH connection", response.data["detail"])

    @responses.activate
    def test_too_large_output_is_a_client_error(self):
        responses.get(
            f"{API_URL}/compute/{SYSTEM}/jobs",
            status=413,
            json={"message": "Output limit exceeded."},
        )
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @responses.activate
    def test_read_is_retried_while_cluster_is_unavailable(self):
        jobs = f"{API_URL}/compute/{SYSTEM}/jobs"
        responses.get(jobs, status=503, json={"message": "busy"})
        responses.get(jobs, json={"jobs": []})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_submit_is_never_retried(self):
        submit = responses.post(
            f"{API_URL}/compute/{SYSTEM}/jobs", status=503, json={"message": "busy"}
        )
        response = self.client.post(
            self.url("jobs"),
            {"name": "hello", "commands": "srun hostname", "time_limit_minutes": 30},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(submit.call_count, 1)

    def test_clients_share_one_session_per_instance(self):
        first = FirecrestClient(API_URL, "a")
        second = FirecrestClient(f"{API_URL}/", "b")
        other = FirecrestClient("http://other-firecrest:5000", "c")
        self.assertIs(first.session, second.session)
        self.assertIsNot(first.session, other.session)

    @responses.activate
    def test_shared_session_keeps_no_cookies_between_users(self):
        jobs = f"{API_URL}/compute/{SYSTEM}/jobs"
        responses.get(
            jobs, json={"jobs": []}, headers={"Set-Cookie": "gw=alice; Path=/"}
        )
        responses.get(jobs, json={"jobs": []})
        FirecrestClient(API_URL, "alice-token").list_jobs(SYSTEM)
        FirecrestClient(API_URL, "bob-token").list_jobs(SYSTEM)
        self.assertNotIn("Cookie", responses.calls[1].request.headers)


class IdentityTest(HpcWorkspaceTestBase):
    def setUp(self):
        super().setUp()
        cache.delete(identity_cache_key(self.user, self.offering, ACCOUNT))

    def userinfo(self, name=USERNAME, accounts=({"name": ACCOUNT, "default": True},)):
        return responses.get(
            f"{API_URL}/status/{SYSTEM}/userinfo",
            json={
                "user": {"id": "9000", "name": name},
                "groups": [],
                "accounts": list(accounts) if accounts is not None else None,
            },
        )

    @responses.activate
    def test_matching_identity_is_checked_once(self):
        userinfo = self.userinfo()
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobs": []})
        self.assertEqual(self.client.get(self.url("jobs")).status_code, 200)
        self.assertEqual(self.client.get(self.url("jobs")).status_code, 200)
        self.assertEqual(userinfo.call_count, 1)

    @responses.activate
    def test_other_cluster_login_is_refused(self):
        self.userinfo(name="someone_else")
        jobs = responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobs": []})
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("someone_else", str(response.data))
        self.assertIn(USERNAME, str(response.data))
        self.assertEqual(jobs.call_count, 0)

    @responses.activate
    def test_missing_slurm_association_is_explained_and_rechecked(self):
        userinfo = self.userinfo(accounts=[{"name": "other_alloc", "default": True}])
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(f"Slurm account '{ACCOUNT}'", str(response.data))
        self.client.get(self.url("jobs"))
        self.assertEqual(userinfo.call_count, 2)

    @responses.activate
    def test_unlisted_accounts_are_not_held_against_the_user(self):
        self.userinfo(accounts=None)
        responses.get(f"{API_URL}/compute/{SYSTEM}/jobs", json={"jobs": []})
        self.assertEqual(self.client.get(self.url("jobs")).status_code, 200)

    @responses.activate
    def test_overview_reports_identity_mismatch_inline(self):
        self.userinfo(name="someone_else")
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("account mapping", response.data["error"])

    @responses.activate
    def test_account_not_ready_is_explained(self):
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()
        response = self.client.get(self.url("jobs"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not ready (Creating)", str(response.data))


class SoftwareTest(HpcWorkspaceTestBase):
    def test_software_filtered_by_partition_architecture(self):
        catalog = factories.SoftwareCatalogFactory(name="EESSI", version="2023.06")
        partition = factories.OfferingPartitionFactory(
            offering=self.offering, partition_name="arm", cpu_arch="aarch64/neoverse_v1"
        )
        marketplace_models.OfferingSoftwareCatalog.objects.create(
            offering=self.offering, catalog=catalog
        )
        package = marketplace_models.SoftwarePackage.objects.create(
            catalog=catalog, name="GROMACS"
        )
        for version, arch in (("2024.1", "aarch64"), ("2023.3", "x86_64")):
            sv = marketplace_models.SoftwareVersion.objects.create(
                package=package,
                version=version,
                module_version=f"{version}-foss-2023b",
            )
            marketplace_models.SoftwareTarget.objects.create(
                version=sv, target_type="cpu_architecture", target_name=arch
            )

        response = self.client.get(
            self.url("software"),
            {"query": "gromacs", "partition": partition.partition_name},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["version"], "2024.1")
        self.assertEqual(response.data[0]["setup"], ["module load EESSI/2023.06"])
        self.assertEqual(
            response.data[0]["load"], "module load GROMACS/2024.1-foss-2023b"
        )
