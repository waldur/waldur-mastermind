from dataclasses import asdict

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.auth_utils import is_delegated
from waldur_mastermind.marketplace import models as marketplace_models

from . import charts, pdf, scripts, serializers
from .client import FirecrestException
from .monitoring import MonitorException
from .workspace import HpcWorkspace, WorkspaceUnavailable, resources_for_user

JOB_ID_PATTERN = r"(?P<job_id>[0-9]+)"
JOB_ID_PARAMETER = OpenApiParameter(
    "job_id", str, OpenApiParameter.PATH, description="Slurm job ID"
)
RESOURCE_UUID_PARAMETER = OpenApiParameter(
    "resource_uuid",
    OpenApiTypes.UUID,
    OpenApiParameter.QUERY,
    description="Only the workspace of this resource, e.g. to tell whether a "
    "resource page has one.",
    extensions={"x-waldur-operation-id": "marketplace_resources_retrieve"},
)


class FirecrestUnavailable(exceptions.APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "FirecREST request failed."
    default_code = "firecrest_unavailable"


class SignInRequired(exceptions.APIException):
    """The user's identity provider token is gone; signing in again restores it."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "identity_provider_sign_in_required"

    def __init__(self, message, provider):
        super().__init__(
            {
                "detail": message,
                "code": self.default_code,
                "identity_provider": provider,
            }
        )


def raise_for_workspace(error: WorkspaceUnavailable):
    if error.sign_in_provider:
        raise SignInRequired(str(error), error.sign_in_provider)
    raise exceptions.ValidationError(str(error))


def raise_for_firecrest(error: FirecrestException):
    if error.status_code == status.HTTP_404_NOT_FOUND:
        raise exceptions.NotFound(str(error))
    if error.status_code == status.HTTP_401_UNAUTHORIZED and error.sign_in_provider:
        raise SignInRequired(str(error), error.sign_in_provider)
    if error.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise exceptions.PermissionDenied(str(error))
    if error.status_code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
    ):
        raise exceptions.ValidationError(str(error))
    raise FirecrestUnavailable(str(error))


def raise_for_monitor(error: MonitorException):
    if error.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise exceptions.PermissionDenied(str(error))
    raise FirecrestUnavailable(str(error))


class IsOwnSignIn(permissions.BasePermission):
    """The workspace acts on the cluster as the user, with their identity
    provider token, and no permission scopes that down. So only the user's own
    sign-in may use it, not a personal access token, an identity provider
    token or impersonation (see ``is_delegated``)."""

    message = "The HPC workspace is only available to your own sign-in."

    def has_permission(self, request, view):
        return not is_delegated(request)


class HpcWorkspaceViewSet(viewsets.GenericViewSet):
    permission_classes = [permissions.IsAuthenticated, IsOwnSignIn]
    """SLURM allocations operated through FirecREST v2 as the signed-in user.

    FirecREST acts with the user's own identity provider token, so the cluster
    enforces the user's POSIX permissions and Slurm associations; Waldur adds
    the allocation context and confines every job operation to its account.
    """

    lookup_field = "uuid"
    serializer_class = serializers.HpcWorkspaceSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return marketplace_models.Resource.objects.none()
        return resources_for_user(self.request.user)

    def get_workspace(self) -> HpcWorkspace:
        resource = get_object_or_404(self.get_queryset(), uuid=self.kwargs["uuid"])
        try:
            return HpcWorkspace.for_resource(resource, self.request.user)
        except WorkspaceUnavailable as e:
            raise_for_workspace(e)

    def call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except WorkspaceUnavailable as e:
            raise_for_workspace(e)
        except FirecrestException as e:
            raise_for_firecrest(e)
        except MonitorException as e:
            # Jobs come from the cluster monitor when there is no FirecREST.
            raise_for_monitor(e)

    @staticmethod
    def query(serializer_class, request):
        serializer = serializer_class(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    @extend_schema(
        summary="List HPC workspaces",
        description="SLURM allocations visible to the user whose offering is connected to "
        "FirecREST or a cluster monitor.",
        parameters=[RESOURCE_UUID_PARAMETER],
    )
    def list(self, request):
        params = self.query(serializers.HpcWorkspaceListQuerySerializer, request)
        queryset = self.get_queryset()
        if params.get("resource_uuid"):
            queryset = queryset.filter(uuid=params["resource_uuid"])
        page = self.paginate_queryset(queryset)
        serializer = serializers.HpcWorkspaceSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        summary="HPC workspace overview",
        description="Allocation, the user's cluster identity and FirecREST system health. "
        "Problems reaching FirecREST are reported in 'error' rather than failing the request; "
        "'sign_in_provider' names the identity provider to sign in through again when that "
        "is the remedy. Without FirecREST ('firecrest_enabled' false) the health is left out.",
        responses={200: serializers.HpcWorkspaceOverviewSerializer},
    )
    def retrieve(self, request, uuid=None):
        resource = get_object_or_404(self.get_queryset(), uuid=uuid)
        data = serializers.HpcWorkspaceSerializer(resource).data
        data.update(
            username=None,
            home_directory=None,
            healthy=None,
            services_health=None,
            error=None,
            sign_in_provider=None,
        )
        try:
            workspace = HpcWorkspace.for_resource(resource, request.user)
            data["username"] = workspace.username
            if not workspace.has_firecrest:
                # What every job call needs, checked without calling the
                # monitor, so the overview shows the remedy (e.g. signing in).
                workspace.check_monitor_access()
                return Response(data)
            data["home_directory"] = workspace.home_directory
            system = workspace.system_status()
            health = system.get("servicesHealth") or []
            data["services_health"] = serializers.HpcServiceHealthSerializer(
                health, many=True
            ).data
            data["healthy"] = all(item.get("healthy") for item in health)
        except WorkspaceUnavailable as e:
            data["error"] = str(e)
            data["sign_in_provider"] = e.sign_in_provider
        except FirecrestException as e:
            data["error"] = str(e)
            data["sign_in_provider"] = e.sign_in_provider
        return Response(data)

    @extend_schema(
        summary="Partitions of the cluster",
        description="Partitions the provider publishes in Waldur (limits, QoS, architecture) "
        "merged with what the cluster reports live through FirecREST (node counts).",
        responses={200: serializers.HpcPartitionSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def partitions(self, request, uuid=None):
        workspace = self.get_workspace()
        data = self.call(workspace.partitions)
        return Response(serializers.HpcPartitionSerializer(data, many=True).data)

    @extend_schema(
        methods=["GET"],
        summary="List jobs of the allocation",
        parameters=[serializers.HpcJobListQuerySerializer],
        responses={200: serializers.HpcJobSerializer(many=True)},
    )
    @extend_schema(
        methods=["POST"],
        summary="Submit a job to the allocation",
        description="Renders the batch script from the parameters, checks them against "
        "the published partition and QoS limits and submits it under the allocation's account.",
        request=serializers.HpcJobSpecSerializer,
        responses={201: serializers.HpcJobSubmitResponseSerializer},
    )
    @action(detail=True, methods=["get", "post"], pagination_class=None)
    def jobs(self, request, uuid=None):
        workspace = self.get_workspace()
        if request.method == "GET":
            params = self.query(serializers.HpcJobListQuerySerializer, request)
            jobs = self.call(workspace.list_jobs, params.get("time_window"))
            return Response(serializers.HpcJobSerializer(jobs, many=True).data)

        serializer = serializers.HpcJobSpecSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = self.call(
            workspace.submit, scripts.JobSpec(**serializer.validated_data)
        )
        return Response(
            serializers.HpcJobSubmitResponseSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Preview a batch script",
        description="Renders the batch script for the parameters and validates them, without submitting.",
        request=serializers.HpcJobSpecSerializer,
        responses={200: serializers.HpcScriptPreviewSerializer},
    )
    @action(detail=True, methods=["post"])
    def preview_script(self, request, uuid=None):
        workspace = self.get_workspace()
        serializer = serializers.HpcJobSpecSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        spec = scripts.JobSpec(**serializer.validated_data)
        validation = workspace.validate(spec)
        data = {
            "script": scripts.build_script(spec, workspace.account),
            "errors": validation.errors,
            "warnings": validation.warnings,
        }
        return Response(serializers.HpcScriptPreviewSerializer(data).data)

    @extend_schema(
        summary="Get a job of the allocation",
        parameters=[JOB_ID_PARAMETER],
        responses={200: serializers.HpcJobSerializer},
    )
    @action(detail=True, methods=["get"], url_path=f"jobs/{JOB_ID_PATTERN}")
    def job(self, request, uuid=None, job_id=None):
        workspace = self.get_workspace()
        job = self.call(workspace.get_job, job_id)
        return Response(serializers.HpcJobSerializer(job).data)

    @extend_schema(
        summary="Get the script and output of a job",
        description="The batch script and the last lines of standard output and error.",
        parameters=[JOB_ID_PARAMETER, serializers.HpcJobOutputQuerySerializer],
        responses={200: serializers.HpcJobOutputSerializer},
    )
    @action(detail=True, methods=["get"], url_path=f"jobs/{JOB_ID_PATTERN}/output")
    def job_output(self, request, uuid=None, job_id=None):
        workspace = self.get_workspace()
        params = self.query(serializers.HpcJobOutputQuerySerializer, request)
        data = self.call(workspace.job_output, job_id, params["lines"])
        return Response(serializers.HpcJobOutputSerializer(data).data)

    @extend_schema(
        summary="Get the performance of a job over its run",
        description="Per-GPU use, GPU memory and power, CPU cores and host memory "
        "over the job's run, and its busiest processes, from the cluster monitor "
        "(slurm-monitor with Sonar probes) as the signed-in user.",
        parameters=[JOB_ID_PARAMETER],
        responses={200: serializers.HpcJobPerformanceSerializer},
    )
    @action(detail=True, methods=["get"], url_path=f"jobs/{JOB_ID_PATTERN}/performance")
    def job_performance(self, request, uuid=None, job_id=None):
        workspace = self.get_workspace()
        data = self.call(workspace.job_performance, job_id)
        return Response(serializers.HpcJobPerformanceSerializer(data).data)

    @extend_schema(
        summary="Download the performance report of a job as PDF",
        description="The job's accounting, summary figures, charts and processes "
        "from the cluster monitor, as one PDF. Without monitor samples the report "
        "keeps the accounting and says why.",
        parameters=[JOB_ID_PARAMETER],
        responses={(200, "application/pdf"): OpenApiTypes.BINARY},
    )
    @action(
        detail=True,
        methods=["get"],
        url_path=f"jobs/{JOB_ID_PATTERN}/performance_pdf",
    )
    def job_performance_pdf(self, request, uuid=None, job_id=None):
        workspace = self.get_workspace()
        job, performance = self.call(workspace.job_report, job_id)
        content = pdf.job_report_pdf(
            serializers.HpcJobSerializer(job).data,
            performance,
            workspace.resource.name,
        )
        response = HttpResponse(content, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="job-{job_id}-report.pdf"'
        )
        return response

    @extend_schema(
        summary="Cancel a job of the allocation",
        request=None,
        parameters=[JOB_ID_PARAMETER],
        responses={204: None},
    )
    @action(detail=True, methods=["post"], url_path=f"jobs/{JOB_ID_PATTERN}/cancel")
    def cancel_job(self, request, uuid=None, job_id=None):
        workspace = self.get_workspace()
        self.call(workspace.cancel, job_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="List a directory on the cluster",
        parameters=[serializers.HpcFileListQuerySerializer],
        responses={200: serializers.HpcFileSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def files(self, request, uuid=None):
        workspace = self.get_workspace()
        params = self.query(serializers.HpcFileListQuerySerializer, request)
        files = self.call(
            workspace.list_directory, params.get("path"), params["show_hidden"]
        )
        return Response(serializers.HpcFileSerializer(files, many=True).data)

    @extend_schema(
        summary="View a file on the cluster",
        description="Content of a small text file (bounded by FirecREST's operation size limit).",
        parameters=[serializers.HpcFileContentQuerySerializer],
        responses={200: serializers.HpcFileContentSerializer},
    )
    @action(detail=True, methods=["get"])
    def file_content(self, request, uuid=None):
        workspace = self.get_workspace()
        params = self.query(serializers.HpcFileContentQuerySerializer, request)
        content = self.call(workspace.view_file, params["path"])
        return Response(
            serializers.HpcFileContentSerializer(
                {"path": params["path"], "content": content}
            ).data
        )

    @extend_schema(
        summary="Find software available on the cluster",
        description="Searches the offering's software catalogs (EESSI, Spack, ...); with a "
        "partition, only builds for its CPU architecture. Returns the lines a batch script needs.",
        parameters=[serializers.HpcSoftwareQuerySerializer],
        responses={200: serializers.HpcSoftwareSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def software(self, request, uuid=None):
        workspace = self.get_workspace()
        params = self.query(serializers.HpcSoftwareQuerySerializer, request)
        matches = workspace.find_software(params["query"], params["partition"])
        return Response(
            serializers.HpcSoftwareSerializer(
                [asdict(m) for m in matches], many=True
            ).data
        )

    @extend_schema(
        summary="Job statistics of the allocation",
        description="Outcomes per day, failures by exit code, time limit fit, CPU and "
        "memory efficiency, queue waits by partition and right-sizing suggestions "
        "over the last days, with chart specs for each figure.",
        parameters=[serializers.HpcJobStatsQuerySerializer],
        responses={200: serializers.HpcJobStatsSerializer},
    )
    @action(detail=True, methods=["get"])
    def job_stats(self, request, uuid=None):
        workspace = self.get_workspace()
        params = self.query(serializers.HpcJobStatsQuerySerializer, request)
        stats = self.call(workspace.job_stats, params["days"])
        stats["charts"] = [
            charts.outcomes(stats),
            charts.job_timeline(stats),
            charts.walltime_fit(stats),
            charts.efficiency(stats),
            charts.queue_wait(stats),
        ]
        return Response(serializers.HpcJobStatsSerializer(stats).data)

    @extend_schema(
        summary="Node load and reservations of the cluster",
        description="Every node with its state, CPU load and partitions, a summary per "
        "partition and the upcoming reservations and maintenance windows.",
        responses={200: serializers.HpcClusterLoadSerializer},
    )
    @action(detail=True, methods=["get"])
    def cluster_load(self, request, uuid=None):
        workspace = self.get_workspace()
        load = self.call(workspace.cluster_load)
        load["charts"] = [charts.node_map(load), charts.reservations(load)]
        return Response(serializers.HpcClusterLoadSerializer(load).data)

    @extend_schema(
        summary="Usage of the allocation against its limits",
        description="Consumption of each limited component in the current limit period "
        "from Waldur's usage records, per user, with a run-out forecast at the recent pace.",
        responses={200: serializers.HpcAllocationUsageSerializer},
    )
    @action(detail=True, methods=["get"])
    def usage(self, request, uuid=None):
        workspace = self.get_workspace()
        usage = workspace.allocation_usage()
        usage["charts"] = [
            chart
            for component in usage["components"]
            for chart in (
                charts.allocation_burn(component),
                charts.usage_by_user(component),
            )
        ]
        return Response(serializers.HpcAllocationUsageSerializer(usage).data)

    @extend_schema(
        summary="Plot a job output file",
        description="Reads a GROMACS .xvg, a CSV/TSV table or a WRF rsl log from the "
        "cluster and returns its series as a chart spec, with a short summary.",
        parameters=[serializers.HpcOutputSeriesQuerySerializer],
        responses={200: serializers.HpcOutputSeriesSerializer},
    )
    @action(detail=True, methods=["get"])
    def output_series(self, request, uuid=None):
        workspace = self.get_workspace()
        params = self.query(serializers.HpcOutputSeriesQuerySerializer, request)
        data = self.call(
            workspace.output_series, params["path"], params.get("forecast_hours")
        )
        data["chart"] = charts.output_series(data)
        return Response(serializers.HpcOutputSeriesSerializer(data).data)
