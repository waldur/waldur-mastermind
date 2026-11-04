"""An HPC workspace: one SLURM allocation in Waldur, operated through FirecREST.

Waldur contributes what FirecREST does not know -- which allocation (Slurm
account) the work is charged to, the partitions and QoS the provider
publishes, the software catalogs -- and FirecREST does the work on the
cluster as the user. Every job operation is scoped to the allocation's
account, so a workspace never shows or touches the user's jobs under other
allocations.

An offering connected only to a cluster monitor (slurm-monitor) gets a
read-only workspace: its jobs, their performance and reports, job
statistics and the software catalogs. Submitting, cancelling, files and the
cluster's live state need FirecREST.

Shared by the REST views and the AI assistant tools.
"""

import time
from dataclasses import dataclass
from functools import cached_property

import jwt
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from waldur_auth_social import oauth_tokens
from waldur_core.configuration import config
from waldur_mastermind.marketplace import maintenance_reservations
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OfferingUserStates, ResourceStates

from . import allowlist, insights, monitoring, outputs, scripts, software
from .client import FirecrestClient, FirecrestException

API_URL_OPTION = "firecrest_api_url"
SYSTEM_NAME_OPTION = "firecrest_system_name"

# How long a user's cluster identity, once it agrees with Waldur's, is
# trusted before FirecREST is asked again.
IDENTITY_CACHE_SECONDS = 600


def identity_cache_key(user, offering, account) -> str:
    return f"waldur_firecrest:identity:{user.uuid.hex}:{offering.uuid.hex}:{account}"


# Job history comes from FirecREST's own windows only (1h … 7d): a longer one
# would make the cluster list every job of a month on each Insights view.
HISTORY_WINDOW = "7d"
MAX_INSIGHTS_DAYS = 7

# FirecREST's windows, for reading the same history from a cluster monitor.
WINDOW_SECONDS = {
    "1h": 3600,
    "8h": 8 * 3600,
    "24h": 86400,
    "3d": 3 * 86400,
    "7d": 7 * 86400,
}
DEFAULT_WINDOW = "24h"

NEEDS_FIRECREST = (
    "This offering is connected to a cluster monitor only: its jobs can be "
    "followed here, but running, canceling and opening files on the cluster "
    "need FirecREST."
)

ACTIVE_RESOURCE_STATES = (
    ResourceStates.OK,
    ResourceStates.UPDATING,
    ResourceStates.ERRED,
)


class WorkspaceUnavailable(Exception):
    """The workspace cannot be used, for a reason the user can act on.

    ``sign_in_provider`` is set when signing in again through that identity
    provider is the remedy (see ``tokens.TokenUnavailable``).
    """

    def __init__(self, message, sign_in_provider=None):
        super().__init__(message)
        self.sign_in_provider = sign_in_provider


def allowed_api_urls() -> set[str]:
    return set(allowlist.parse(config.FIRECREST_ALLOWED_API_URLS))


def token_audience(api_url) -> allowlist.TokenAudience | None:
    return allowlist.parse(config.FIRECREST_ALLOWED_API_URLS).get(api_url.rstrip("/"))


def is_firecrest_enabled(offering) -> bool:
    """The offering names a FirecREST endpoint that staff have allowed.

    The URL comes from the provider's offering settings, while the token sent
    to it is the user's own identity provider token: without the allowlist a
    provider could collect the tokens of everyone using its offering.
    """
    options = offering.plugin_options or {}
    api_url = (options.get(API_URL_OPTION) or "").rstrip("/")
    return bool(
        api_url and options.get(SYSTEM_NAME_OPTION) and api_url in allowed_api_urls()
    )


def is_configured() -> bool:
    """Staff have allowed at least one FirecREST or cluster monitor endpoint."""
    return bool(allowed_api_urls() or monitoring.allowed_api_urls())


def is_workspace_enabled(offering) -> bool:
    """FirecREST or a cluster monitor, either allowed by staff, serves it."""
    return is_firecrest_enabled(offering) or monitoring.is_monitoring_enabled(offering)


def _url_lookup(option, urls) -> Q:
    return Q(
        **{
            f"offering__plugin_options__{option}__in": [
                *urls,
                *(f"{url}/" for url in urls),
            ]
        }
    )


def resources_for_user(user):
    """Allocations the user can see whose offering is reachable through FirecREST
    or a cluster monitor.

    Same visibility as the consumer resource list (RESOURCE.LIST on the
    project or organization, or a direct resource role), so a workspace is
    never offered for a resource the user cannot open.
    """
    return (
        marketplace_models.Resource.objects.all()
        .filter_for_service_consumer(user)
        .filter(
            _url_lookup(API_URL_OPTION, allowed_api_urls())
            | _url_lookup(monitoring.API_URL_OPTION, monitoring.allowed_api_urls()),
            state__in=ACTIVE_RESOURCE_STATES,
        )
        .select_related("offering", "project", "project__customer")
        .order_by("name", "pk")
    )


def is_job_id(value) -> bool:
    """A Slurm job id: ASCII digits, as the API routes accept (str.isdigit
    alone also takes other scripts' digits)."""
    value = str(value)
    return value.isascii() and value.isdigit()


def _newest_first_key(job):
    """Newest first: by submission, then id (array tasks after their parent).

    Slurm ids grow with submission on one controller, but not across a
    controller reset or imported history, so the time decides.
    """
    job_id = str(job.get("jobId") or "")
    head, _, tail = job_id.partition("_")
    submitted = (job.get("time") or {}).get("submission") or 0
    return (submitted, int(head) if head.isdigit() else -1, tail)


def with_elapsed(job: dict) -> dict:
    """Fill in the run time when FirecREST leaves it out.

    FirecREST reports ``time.elapsed`` only from accounting; jobs it reads
    from slurmctld come back with ``elapsed=None`` and just start/end.
    """
    times = job.get("time") or {}
    start = times.get("start")
    state = ((job.get("status") or {}).get("state") or "").upper()
    if times.get("elapsed") is not None or not start or state == "PENDING":
        return job
    end = times.get("end")
    if state in ("RUNNING", "COMPLETING") or not end:
        end = time.time()
    return {**job, "time": {**times, "elapsed": max(0, int(end - start))}}


@dataclass
class HpcWorkspace:
    resource: marketplace_models.Resource
    user: object

    @classmethod
    def for_resource(cls, resource, user) -> "HpcWorkspace":
        if not is_workspace_enabled(resource.offering):
            raise WorkspaceUnavailable(
                "This offering is not connected to an allowed FirecREST endpoint "
                "or cluster monitor."
            )
        if not resource.backend_id:
            raise WorkspaceUnavailable(
                "The allocation has not been created on the cluster yet."
            )
        return cls(resource=resource, user=user)

    @property
    def offering(self):
        return self.resource.offering

    @cached_property
    def has_firecrest(self) -> bool:
        return is_firecrest_enabled(self.offering)

    @cached_property
    def has_monitor(self) -> bool:
        return monitoring.is_monitoring_enabled(self.offering)

    @property
    def api_url(self) -> str:
        return self.offering.plugin_options[API_URL_OPTION]

    @property
    def system(self) -> str:
        return self.offering.plugin_options[SYSTEM_NAME_OPTION]

    @property
    def account(self) -> str:
        return self.resource.backend_id

    @cached_property
    def username(self) -> str:
        offering_user = (
            marketplace_models.OfferingUser.objects.filter(
                offering=self.offering, user=self.user
            )
            .exclude(username="")
            .exclude(username__isnull=True)
            .first()
        )
        if offering_user is None:
            raise WorkspaceUnavailable(
                "You do not have an account on this cluster yet. "
                "It is created once you are a member of the allocation's project."
            )
        if offering_user.state != OfferingUserStates.OK:
            raise WorkspaceUnavailable(
                f"Your account {offering_user.username} on this cluster is not ready "
                f"({offering_user.get_state_display()}). The workspace opens once "
                "the provider has finished setting it up."
            )
        return offering_user.username

    @property
    def _on_behalf_of(self) -> str:
        return f"user {self.user.uuid.hex} for offering {self.offering.uuid.hex}"

    def _access_token(self, audience: allowlist.TokenAudience | None) -> str:
        try:
            if audience is None:
                return oauth_tokens.get_access_token(self.user)
            return oauth_tokens.get_access_token(
                self.user, audience=audience.audience, scope=audience.scope
            )
        except oauth_tokens.TokenUnavailable as e:
            raise WorkspaceUnavailable(str(e), e.sign_in_provider)

    def require_firecrest(self):
        """Refuse what only FirecREST can do: running and canceling jobs,
        reading their output and files, the cluster's live state."""
        if not self.has_firecrest:
            raise WorkspaceUnavailable(NEEDS_FIRECREST)

    @cached_property
    def client(self) -> FirecrestClient:
        self.require_firecrest()
        access_token = self._access_token(token_audience(self.api_url))
        client = FirecrestClient(
            self.api_url,
            access_token,
            sign_in_provider=oauth_tokens.get_sign_in_provider(self.user),
            on_behalf_of=self._on_behalf_of,
        )
        self._check_identity(client)
        return client

    def _check_identity(self, client: FirecrestClient):
        """Refuse to act when FirecREST's view of the user disagrees with Waldur's.

        FirecREST logs in as whoever its token claim names, while Waldur
        charges work to the allocation account and starts in the home of
        OfferingUser.username. If they disagree, jobs would run under another
        login, or every submit would fail with an unexplained Slurm error.
        Only a passing check is cached, so an account mapping or association
        that is still being set up is checked again on the next request.
        """
        # The account must be ready before FirecREST is asked about it.
        username = self.username
        key = identity_cache_key(self.user, self.offering, self.account)
        if cache.get(key):
            return
        info = client.get_userinfo(self.system)
        login = (info.get("user") or {}).get("name") or ""
        if login != username:
            raise WorkspaceUnavailable(
                f"FirecREST logs you in to the cluster as '{login}', but your account "
                f"for this allocation is '{username}'. Ask the service provider "
                "to correct the account mapping."
            )
        accounts = info.get("accounts")
        # FirecREST leaves accounts out when the scheduler cannot list them.
        if accounts is not None and (self.account or "").lower() not in {
            (item.get("name") or "").lower() for item in accounts
        }:
            raise WorkspaceUnavailable(
                f"Your cluster account '{login}' is not a member of the allocation's "
                f"Slurm account '{self.account}' yet. It is added once the provider "
                "has synchronised the allocation's members."
            )
        cache.set(key, True, IDENTITY_CACHE_SECONDS)

    def check_monitor_access(self) -> str:
        """The user's token for the cluster monitor, without calling it.

        Raises as the monitor calls would: no usable sign-in, or, without
        FirecREST, a token naming another login than the allocation's.
        """
        if not self.has_monitor:
            raise WorkspaceUnavailable(
                "This offering is not connected to an allowed cluster monitor."
            )
        access_token = self._access_token(
            monitoring.token_audience(
                self.offering.plugin_options[monitoring.API_URL_OPTION]
            )
        )
        if not self.has_firecrest:
            self._check_monitor_identity(access_token)
        return access_token

    @cached_property
    def monitor(self) -> monitoring.MonitorClient:
        access_token = self.check_monitor_access()
        options = self.offering.plugin_options
        return monitoring.MonitorClient(
            options[monitoring.API_URL_OPTION],
            options[monitoring.CLUSTER_OPTION],
            access_token,
            on_behalf_of=self._on_behalf_of,
        )

    def _check_monitor_identity(self, access_token):
        """Without FirecREST, the cluster monitor alone says who the user is.

        slurm-monitor shows the jobs of the user its token's
        ``preferred_username`` names; if that is not the allocation's login,
        the workspace would show nothing, or someone else's jobs.
        """
        username = self.username
        try:
            claims = jwt.decode(access_token, options={"verify_signature": False})
        except jwt.PyJWTError:
            # An opaque token: the monitor maps it, Waldur cannot check it.
            return
        login = claims.get("preferred_username") if isinstance(claims, dict) else None
        if login and login != username:
            raise WorkspaceUnavailable(
                f"The cluster monitor knows you as '{login}', but your account for "
                f"this allocation is '{username}'. Ask the service provider to "
                "correct the account mapping."
            )

    def job_performance(self, job_id) -> dict:
        """The job's samples from the cluster monitor; the job read (FirecREST,
        or the monitor itself) proves it is ours."""
        return monitoring.job_performance(self.monitor, self.get_job(job_id))

    def job_report(self, job_id) -> tuple[dict, dict]:
        """The job and its performance, for the PDF report.

        With FirecREST, the report always has the job's accounting; a cluster
        monitor, when the offering has one, adds the time series, and one that
        cannot answer leaves a note instead of failing the download. Without
        FirecREST, the job itself comes from the monitor.
        """
        job = self.get_job(job_id)
        if not self.has_monitor:
            return job, monitoring.unavailable("")
        try:
            performance = monitoring.job_performance(self.monitor, job)
        except (monitoring.MonitorException, WorkspaceUnavailable) as e:
            performance = monitoring.unavailable(str(e))
        return job, performance

    @property
    def home_directory(self) -> str:
        return f"/home/{self.username}"

    # Cluster

    def system_status(self) -> dict:
        for system in self.client.list_systems():
            if system.get("name", "").lower() == self.system.lower():
                return system
        raise WorkspaceUnavailable(
            f"FirecREST does not serve the system '{self.system}'."
        )

    def partitions(self) -> list[dict]:
        """Published partitions (Waldur) merged with live state (FirecREST)."""
        live, nodes = {}, []
        if self.has_firecrest:
            live = {p["name"]: p for p in self.client.list_partitions(self.system)}
            nodes = self.client.list_nodes(self.system)
        node_counts = {}
        for node in nodes:
            states = set(node.get("state") or [])
            for name in node.get("partitions") or []:
                counts = node_counts.setdefault(name, {"total": 0, "idle": 0})
                counts["total"] += 1
                if states == {"IDLE"}:
                    counts["idle"] += 1

        published = {
            p.partition_name: p
            for p in marketplace_models.OfferingPartition.objects.filter(
                offering=self.offering
            ).prefetch_related("qos_options__qos")
        }
        result = []
        for name in sorted(set(published) | set(live)):
            partition = published.get(name)
            counts = node_counts.get(name, {})
            result.append(
                {
                    "name": name,
                    "published": partition is not None,
                    "live": name in live,
                    "total_nodes": live.get(name, {}).get("totalNodes")
                    or counts.get("total"),
                    "idle_nodes": counts.get("idle"),
                    "cpus": live.get(name, {}).get("cpus"),
                    "max_time": partition.max_time if partition else None,
                    "default_time": partition.default_time if partition else None,
                    "max_nodes": partition.max_nodes if partition else None,
                    "max_cpus_per_node": partition.max_cpus_per_node
                    if partition
                    else None,
                    "max_mem_per_node": partition.max_mem_per_node
                    if partition
                    else None,
                    "cpu_arch": partition.cpu_arch if partition else "",
                    "gpu_arch": partition.gpu_arch if partition else "",
                    "qos": [link.qos.name for link in partition.qos_options.all()]
                    if partition
                    else [],
                    "default_qos": next(
                        (
                            link.qos.name
                            for link in partition.qos_options.all()
                            if link.is_default
                        ),
                        "",
                    )
                    if partition
                    else "",
                }
            )
        return result

    # Jobs

    def list_jobs(self, time_window=None) -> list[dict]:
        if self.has_firecrest:
            jobs = self.client.list_jobs(
                self.system, account=self.account, time_window=time_window
            )
        else:
            now = time.time()
            seconds = WINDOW_SECONDS[time_window or DEFAULT_WINDOW]
            jobs = monitoring.jobs_from_rows(self.monitor.list_jobs(now - seconds, now))
        # The account filter is passed on, but not every scheduler path
        # applies it (FirecREST merges active jobs that are not filtered),
        # and the workspace boundary must not depend on that.
        own = [
            insights.with_metrics(with_elapsed(job))
            for job in jobs
            if self._is_own(job)
        ]
        return sorted(own, key=_newest_first_key, reverse=True)

    def _is_own(self, job) -> bool:
        if (job.get("account") or "").lower() != self.account.lower():
            return False
        # The monitor scopes jobs to the token's user; holding it to the
        # allocation's login keeps that so whatever the monitor returns.
        return self.has_firecrest or job.get("user") == self.username

    def get_job(self, job_id) -> dict:
        if self.has_firecrest:
            job = self.client.get_job(self.system, job_id)
        else:
            job = self._monitor_job(job_id)
        if not self._is_own(job):
            # Same answer as a missing job: the workspace is the allocation.
            raise FirecrestException(f"Job {job_id} is not found.", status_code=404)
        return insights.with_metrics(with_elapsed(job))

    def _monitor_job(self, job_id) -> dict:
        record = None
        if is_job_id(job_id):
            try:
                record = self.monitor.get_job(job_id)
            except monitoring.MonitorException as e:
                # slurm-monitor's answer for a job it does not have, or one of
                # another user's.
                if e.status_code != 404:
                    raise
        if not record:
            raise FirecrestException(f"Job {job_id} is not found.", status_code=404)
        job = monitoring.job_from_rows([record])
        # The single job read has no accounting; the job list over its run has.
        start = job["time"]["start"]
        if start:
            rows = [
                row
                for row in self.monitor.list_jobs(
                    start, job["time"]["end"] or time.time()
                )
                if row.get("job_id") == record.get("job_id")
            ]
            if rows:
                job = monitoring.job_from_rows(rows)
        return job

    def job_output(self, job_id, lines=100) -> dict:
        # FirecREST first: without it the job would come from the cluster
        # monitor, which has no output to read.
        client = self.client
        job = self.get_job(job_id)
        metadata = client.get_job_metadata(self.system, job_id)
        result = {"job": job, "script": metadata.get("script")}
        for stream in ("standardOutput", "standardError"):
            path = metadata.get(stream)
            content = None
            if path:
                if not path.startswith("/"):
                    path = f"{job.get('workingDirectory', '').rstrip('/')}/{path}"
                try:
                    content = client.tail_file(self.system, path, lines).get("content")
                except FirecrestException:
                    content = None  # not written yet (pending job) or removed
            result[stream] = {"path": path, "content": content}
        return result

    def validate(self, spec: scripts.JobSpec) -> scripts.Validation:
        """Limits Waldur publishes, plus maintenance the job would run into."""
        validation = scripts.validate(spec, self.offering)
        try:
            load = self.cluster_load()
            validation.warnings += insights.maintenance_conflicts(
                load["reservations"],
                spec.partition,
                spec.time_limit_minutes,
                announcements=load["announcements"],
            )
        except (FirecrestException, WorkspaceUnavailable):
            pass  # reservations are advisory; never block a submission on them
        return validation

    def submit(self, spec: scripts.JobSpec) -> dict:
        client = self.client
        validation = self.validate(spec)
        if not validation.is_valid:
            raise WorkspaceUnavailable(" ".join(validation.errors))
        working_directory = spec.working_directory or self.home_directory
        script = scripts.build_script(spec, self.account)
        job = {
            "name": spec.name,
            "account": self.account,
            "workingDirectory": working_directory,
            "standardOutput": f"{working_directory.rstrip('/')}/{spec.name}-%j.out",
            "standardError": f"{working_directory.rstrip('/')}/{spec.name}-%j.err",
            "script": script,
            "env": spec.environment or {},
            # slurmrestd does not read #SBATCH lines (sbatch does), so the
            # request also travels as job description fields (FireCREST
            # >= 2.6.2). Slurm derives the task count from nodes and
            # tasksPerNode.
            "nodes": spec.nodes,
            "tasksPerNode": spec.ntasks_per_node,
            "cpusPerTask": spec.cpus_per_task,
            "timeLimit": spec.time_limit_minutes,
        }
        if spec.memory_mb:
            job["memoryPerNode"] = spec.memory_mb
        if spec.partition:
            job["partition"] = spec.partition
        job_id = client.submit_job(self.system, job)
        return {
            "job_id": job_id,
            "script": script,
            "warnings": validation.warnings,
        }

    def cancel(self, job_id):
        client = self.client
        self.get_job(job_id)
        client.cancel_job(self.system, job_id)

    # Insights

    def job_stats(self, days: int = MAX_INSIGHTS_DAYS) -> dict:
        days = max(1, min(days, MAX_INSIGHTS_DAYS))
        return insights.job_stats(self.list_jobs(HISTORY_WINDOW), days=days)

    def cluster_load(self) -> dict:
        nodes = self.client.list_nodes(self.system)
        try:
            reservations = self.client.list_reservations(self.system)
        except FirecrestException:
            reservations = []
        return insights.cluster_load(nodes, reservations, self.announcements())

    def announcements(self) -> list[dict]:
        """The provider's published maintenance for this offering."""
        sync = maintenance_reservations.is_enabled(self.offering)
        rows = (
            marketplace_models.MaintenanceAnnouncementOffering.objects.filter(
                offering=self.offering,
                maintenance__state__in=maintenance_reservations.RESERVED_STATES,
                maintenance__scheduled_end__gt=timezone.now(),
            )
            .select_related("maintenance")
            .order_by("maintenance__scheduled_start")
        )
        return insights.announcement_rows(
            rows,
            maintenance_reservations.reservation_name,
            lambda row: (
                sync and maintenance_reservations.reserved_partitions(row) is not None
            ),
        )

    def allocation_usage(self) -> dict:
        return insights.allocation_usage(self.resource)

    def output_series(self, path: str, forecast_hours: float | None = None) -> dict:
        if not outputs.kind_for_path(path):
            raise WorkspaceUnavailable(
                "Plots are available for GROMACS .xvg, CSV/TSV tables and WRF rsl logs."
            )
        text = self.client.view_file(self.system, path)
        if outputs.kind_for_path(path) == "wrf_rsl" and forecast_hours is None:
            namelist = f"{path.rsplit('/', 1)[0]}/namelist.input"
            try:
                forecast_hours = outputs.forecast_hours_from_namelist(
                    self.client.view_file(self.system, namelist)
                )
            except FirecrestException:
                forecast_hours = None
        try:
            series = outputs.parse(path, text, forecast_hours)
        except outputs.UnsupportedOutput as e:
            raise WorkspaceUnavailable(str(e))
        return {"path": path, **series.as_dict()}

    # Files

    def list_directory(self, path=None, show_hidden=False):
        return self.client.list_directory(
            self.system, path or self.home_directory, show_hidden=show_hidden
        )

    def view_file(self, path, size=None):
        return self.client.view_file(self.system, path, size=size)

    # Software

    def find_software(self, query="", partition_name="", limit=None):
        partition = scripts.get_partition(self.offering, partition_name)
        return software.find_software(
            self.offering,
            query=query,
            partition=partition,
            limit=limit or software.DEFAULT_LIMIT,
        )
