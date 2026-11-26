from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions as rf_exceptions
from rest_framework import permissions as rf_permissions
from rest_framework import status, views
from rest_framework import viewsets as rf_viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import access
from waldur_core.core import permissions as core_permissions
from waldur_mastermind.demo_presets import gate as demo_presets_gate
from waldur_mastermind.demo_presets import jobs as demo_preset_jobs
from waldur_mastermind.demo_presets import preview as demo_preset_preview
from waldur_mastermind.demo_presets import serializers
from waldur_mastermind.demo_presets import tasks as demo_preset_tasks
from waldur_mastermind.demo_presets.manifest import DemoPresetManager
from waldur_mastermind.marketplace import models


class DemoPresetViewSet(rf_viewsets.GenericViewSet):
    """
    ViewSet for managing demo data presets.

    Provides read-only access to demo presets stored in the repository,
    plus a load action to apply presets to the database.

    Staff access only, and only on deployments that enable
    WALDUR_CORE["DEMO_PRESETS_ENABLED"]; elsewhere every action answers 404.
    """

    # Required for OpenAPI schema generation (no model, using ServiceProvider as placeholder)
    queryset = models.ServiceProvider.objects.none()

    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]
    serializer_class = serializers.DemoPresetSerializer

    def initial(self, request, *args, **kwargs):
        # Before authentication and permissions, so a deployment without demo
        # presets does not reveal the endpoint to anyone.
        if not demo_presets_gate.is_enabled():
            raise rf_exceptions.NotFound()
        super().initial(request, *args, **kwargs)

    @staticmethod
    def _get_allowed_preset_or_404(name):
        preset = (
            DemoPresetManager.get_preset_info(name)
            if demo_presets_gate.is_allowed(name)
            else None
        )
        if not preset:
            raise rf_exceptions.NotFound(f"Preset '{name}' not found")
        return preset

    @extend_schema(
        summary="List demo presets",
        description="Returns the demo data presets this deployment offers. Staff access only; answers 404 unless the deployment enables demo presets.",
        responses={status.HTTP_200_OK: serializers.DemoPresetSerializer(many=True)},
    )
    @action(detail=False, methods=["GET"], url_path="list")
    def list_presets(self, request):
        """List all available demo presets."""
        presets = [
            preset
            for preset in DemoPresetManager.list_presets()
            if demo_presets_gate.is_allowed(preset.name)
        ]
        serializer = serializers.DemoPresetSerializer(presets, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="Preview a demo preset",
        description=(
            "Returns what loading the preset would do: its users, "
            "organizations, projects, resources and offerings, the settings "
            "and features a load with cleanup first would change, and "
            "warnings about what would not work afterwards. Secret values and "
            "the passwords of staff and support users are never returned. "
            "Staff access only."
        ),
        parameters=[
            OpenApiParameter(
                "name",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="Name of the preset",
            ),
        ],
        responses={
            status.HTTP_200_OK: serializers.DemoPresetPreviewSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    @action(detail=False, methods=["GET"], url_path=r"info/(?P<name>[\w_-]+)")
    def get_preset(self, request, name=None):
        """Preview a specific preset."""
        preview = (
            demo_preset_preview.build_preview(name)
            if demo_presets_gate.is_allowed(name)
            else None
        )
        if preview is None:
            raise rf_exceptions.NotFound(f"Preset '{name}' not found")
        return Response(serializers.DemoPresetPreviewSerializer(preview).data)

    @extend_schema(
        summary="Load demo preset",
        description=(
            "Load a demo preset into the database. A load runs in the "
            "background and answers 202 with the load; follow it with "
            "/api/demo-preset-status/. While it runs the rest of the API "
            "answers 503. A load with cleanup first drops every task queued "
            "for the workers. A dry run is answered directly with its output. "
            "Answers 409 while another load runs, and 503 if the task queue "
            "cannot be reached. Staff access only."
        ),
        parameters=[
            OpenApiParameter(
                "name",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="Name of the preset to load",
            ),
        ],
        request=serializers.DemoPresetLoadRequestSerializer,
        responses={
            status.HTTP_200_OK: serializers.DemoPresetLoadResponseSerializer,
            status.HTTP_202_ACCEPTED: serializers.DemoPresetLoadJobSerializer,
            status.HTTP_400_BAD_REQUEST: None,
            status.HTTP_404_NOT_FOUND: None,
            status.HTTP_409_CONFLICT: None,
            status.HTTP_503_SERVICE_UNAVAILABLE: None,
        },
    )
    @action(detail=False, methods=["POST"], url_path=r"load/(?P<name>[\w_-]+)")
    def load_preset(self, request, name=None):
        """Load a preset into the database."""
        self._get_allowed_preset_or_404(name)

        serializer = serializers.DemoPresetLoadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dry_run = serializer.validated_data.get("dry_run", False)
        cleanup_first = serializer.validated_data.get("cleanup_first", True)
        skip_users = serializer.validated_data.get("skip_users", False)
        skip_roles = serializer.validated_data.get("skip_roles", False)

        if not dry_run:
            try:
                job = demo_preset_jobs.start_load(
                    preset=name,
                    options={
                        "cleanup_first": cleanup_first,
                        "skip_users": skip_users,
                        "skip_roles": skip_roles,
                    },
                    requested_by=request.user.username,
                )
            except demo_preset_jobs.LoadInProgress as e:
                return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
            try:
                demo_preset_jobs.dispatch(job, demo_preset_tasks.load_demo_preset.delay)
            except Exception as e:
                return Response(
                    {"detail": f"Could not queue the load: {e}"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return Response(
                serializers.DemoPresetLoadJobSerializer(job).data,
                status=status.HTTP_202_ACCEPTED,
            )

        result = DemoPresetManager.load_preset(
            name=name,
            cleanup_first=cleanup_first,
            dry_run=True,
            skip_users=skip_users,
            skip_roles=skip_roles,
        )

        if not result["success"]:
            raise rf_exceptions.ValidationError(result["message"])

        response_serializer = serializers.DemoPresetLoadResponseSerializer(result)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class DemoPresetStatusView(views.APIView):
    """Whether a demo preset is loading, and who can log in once it is done.

    Public, so a browser whose session the load just deleted can follow it;
    answers 404 unless the deployment enables demo presets. It never lists
    staff or support users, whose passwords would give anyone portal-wide
    access.
    """

    authentication_classes = []
    permission_classes = [rf_permissions.AllowAny]
    access_declarations = {"get": [access.public()]}

    @extend_schema(
        summary="Demo preset load status",
        description=(
            "Reports whether a demo preset is loading and the latest load. Once "
            "that load is done, lists the preset's users without a staff or "
            "support role, with their "
            "passwords for quick login. Public; answers 404 unless the "
            "deployment enables demo presets."
        ),
        responses={
            status.HTTP_200_OK: serializers.DemoPresetStatusSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def get(self, request):
        if not demo_presets_gate.is_enabled():
            raise rf_exceptions.NotFound()
        load = demo_preset_jobs.latest_load()
        loading = bool(load and load.state in models.DemoPresetLoad.States.ACTIVE)
        users = []
        if (
            load
            and load.state == models.DemoPresetLoad.States.DONE
            and not load.options.get("skip_users")
        ):
            users = [
                user
                for user in DemoPresetManager.get_preset_users(load.preset)
                if not user["is_staff"] and not user["is_support"]
            ]
        serializer = serializers.DemoPresetStatusSerializer(
            {"loading": loading, "load": load, "users": users}
        )
        return Response(serializer.data)
