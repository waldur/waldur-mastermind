from constance import config
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    PolymorphicProxySerializer,
    extend_schema,
)
from rest_framework import decorators, exceptions, permissions, status, views
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import (
    has_permission,
    has_permission_on_any_source,
    permission_factory,
)
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.permissions import is_staff
from waldur_mastermind.marketplace import models as marketplace_models

from . import enums, filters, ingest, models, otlp, query, serializers, tasks


class RetentionPolicyViewSet(core_views.ActionsViewSet):
    queryset = models.RetentionPolicy.objects.all()
    serializer_class = serializers.RetentionPolicySerializer
    lookup_field = "uuid"
    unsafe_methods_permissions = [is_staff]

    def perform_destroy(self, instance):
        if instance.name == config.METRICS_DEFAULT_RETENTION_POLICY:
            raise IncorrectStateException(
                _("Cannot remove the default retention policy.")
            )
        if instance.definitions.exists():
            raise IncorrectStateException(
                _("Cannot remove a retention policy that metric definitions use.")
            )
        super().perform_destroy(instance)


def _check_definition_owner(request, owner_customer):
    if request.user.is_staff:
        return
    if owner_customer is None or not has_permission_on_any_source(
        request,
        PermissionEnum.CREATE_OFFERING,
        owner_customer,
        ["*", "serviceprovider"],
    ):
        raise exceptions.PermissionDenied()


class MetricDefinitionViewSet(core_views.ActionsViewSet):
    """The metric catalogue.

    Global definitions are curated by staff; a service provider manages the
    definitions private to it. Everyone can read the global ones.
    """

    queryset = models.MetricDefinition.objects.all().select_related(
        "owner_customer", "retention_policy"
    )
    serializer_class = serializers.MetricDefinitionSerializer
    filterset_class = filters.MetricDefinitionFilter
    filter_backends = (DjangoFilterBackend,)
    lookup_field = "uuid"

    def check_create_permissions(request, view, obj=None):
        owner_uuid = request.data.get("owner_customer")
        owner = None
        if owner_uuid:
            owner = structure_models.Customer.objects.filter(uuid=owner_uuid).first()
            if owner is None:
                raise exceptions.PermissionDenied()
        _check_definition_owner(request, owner)

    def check_owner_permissions(request, view, obj=None):
        if obj is not None:
            _check_definition_owner(request, obj.owner_customer)

    create_permissions = [check_create_permissions]
    update_permissions = partial_update_permissions = destroy_permissions = [
        check_owner_permissions
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        return qs.filter(
            Q(owner_customer__isnull=True)
            | Q(owner_customer__in=get_connected_customers(user))
            | Q(
                owner_customer__serviceprovider__in=query.connected_service_providers(
                    user
                )
            )
        )

    def perform_destroy(self, instance):
        if instance.offering_metrics.exists():
            raise IncorrectStateException(
                _(
                    "Cannot remove a definition that offerings report; deprecate it instead."
                )
            )
        super().perform_destroy(instance)


def _manages_offering(request, offering):
    return has_permission_on_any_source(
        request,
        PermissionEnum.UPDATE_OFFERING,
        offering,
        ingest.OFFERING_PROVIDER_SOURCES,
    )


class OfferingMetricViewSet(core_views.ActionsViewSet):
    """Metrics an offering reports for its resources.

    Providers adopt and manage them; members of projects using the offering
    can read them, since a project's metrics page is labelled from here.
    """

    queryset = models.OfferingMetric.objects.all().select_related(
        "offering", "definition"
    )
    serializer_class = serializers.OfferingMetricSerializer
    filterset_class = filters.OfferingMetricFilter
    filter_backends = (DjangoFilterBackend,)
    lookup_field = "uuid"

    def check_create_permissions(request, view, obj=None):
        if request.user.is_staff:
            return
        offering = marketplace_models.Offering.objects.filter(
            uuid=request.data.get("offering") or None
        ).first()
        if offering is None or not _manages_offering(request, offering):
            raise exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    update_permissions = partial_update_permissions = destroy_permissions = (
        pause_permissions
    ) = resume_permissions = archive_permissions = purge_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["offering", "offering.customer", "offering.customer.serviceprovider"],
        )
    ]

    def get_queryset(self):
        return query.visible_offering_metrics(self.request.user).select_related(
            "offering", "definition"
        )

    def perform_create(self, serializer):
        # Two adoptions of different definitions with the same key would
        # leave ingest choosing between them; serialise adoptions per
        # offering and check again under the lock.
        offering = serializer.validated_data["offering"]
        marketplace_models.Offering.objects.select_for_update().filter(
            pk=offering.pk
        ).first()
        if models.OfferingMetric.objects.filter(
            offering=offering,
            definition__key=serializer.validated_data["definition"].key,
        ).exists():
            raise exceptions.ValidationError(
                {
                    "definition": _(
                        "The offering already reports a metric with this key."
                    )
                }
            )
        serializer.save()

    def perform_destroy(self, instance):
        if instance.series.exists():
            raise IncorrectStateException(
                _("Cannot remove a metric with reported data; archive and purge it.")
            )
        super().perform_destroy(instance)

    @extend_schema(request=None, responses={status.HTTP_202_ACCEPTED: None})
    @decorators.action(detail=True, methods=["post"])
    def purge(self, request, uuid=None):
        """Delete everything an archived metric collected, then the metric."""
        offering_metric = self.get_object()
        if offering_metric.state != enums.OfferingMetricStates.ARCHIVED:
            raise IncorrectStateException(_("Only an archived metric can be purged."))
        transaction.on_commit(
            lambda: tasks.purge_offering_metric.delay(offering_metric.uuid.hex)
        )
        return Response(status=status.HTTP_202_ACCEPTED)

    def _transition(self, allowed, target):
        offering_metric = self.get_object()
        if offering_metric.state not in allowed:
            raise IncorrectStateException(
                _("Cannot do this while the metric is %(state)s.")
                % {"state": offering_metric.state}
            )
        offering_metric.state = target
        offering_metric.save(update_fields=["state", "modified"])
        return Response(
            serializers.OfferingMetricSerializer(offering_metric).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(request=None, responses=serializers.OfferingMetricSerializer)
    @decorators.action(detail=True, methods=["post"])
    def pause(self, request, uuid=None):
        """Refuse new points; what was reported stays visible."""
        States = enums.OfferingMetricStates
        return self._transition([States.ACTIVE], States.PAUSED)

    @extend_schema(request=None, responses=serializers.OfferingMetricSerializer)
    @decorators.action(detail=True, methods=["post"])
    def resume(self, request, uuid=None):
        States = enums.OfferingMetricStates
        return self._transition([States.PAUSED, States.ARCHIVED], States.ACTIVE)

    @extend_schema(request=None, responses=serializers.OfferingMetricSerializer)
    @decorators.action(detail=True, methods=["post"])
    def archive(self, request, uuid=None):
        """Stop collecting and hide the metric from consuming projects."""
        States = enums.OfferingMetricStates
        return self._transition([States.ACTIVE, States.PAUSED], States.ARCHIVED)


def _rejected_response(result):
    return Response(
        serializers.MetricReportResultSerializer(
            {"accepted": result.accepted, "rejected": result.rejected}
        ).data,
        status=status.HTTP_200_OK,
    )


class MetricPointViewSet(core_views.ActionsViewSet):
    """Where services report points for the metrics their offerings adopt."""

    queryset = models.MetricPoint.objects.none()
    serializer_class = serializers.MetricPointSerializer
    disabled_actions = ["list", "retrieve", "update", "partial_update", "destroy"]
    throttle_scope = "metrics_ingest"

    @extend_schema(
        summary="Report metric points",
        description=(
            "Records points for resources against metrics their offerings adopt. "
            "Send one point or a list. A point is identified by resource, "
            "metric, attributes and timestamp: sending it again replaces the "
            "value. The caller must be allowed to report usage for every "
            "resource in the request, or nothing is recorded (403). Other "
            "problems refuse only the affected points, listed by index."
        ),
        request=PolymorphicProxySerializer(
            component_name="MetricPointReport",
            serializers=[
                serializers.MetricPointSerializer,
                serializers.MetricPointSerializer(many=True),
            ],
            resource_type_field_name=None,
            many=False,
        ),
        responses={status.HTTP_200_OK: serializers.MetricReportResultSerializer},
    )
    def create(self, request, *args, **kwargs):
        many = isinstance(request.data, list)
        serializer = self.get_serializer(data=request.data, many=many)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data if many else [serializer.validated_data]
        points = [
            ingest.Point(
                resource_uuid=item["resource"].hex,
                metric=item["metric"],
                timestamp=item["timestamp"],
                value=item["value"],
                attributes=item.get("attributes") or {},
                start_time=item.get("start_time"),
            )
            for item in items
        ]
        try:
            result = ingest.ingest(request, points)
        except ingest.Rejected as e:
            raise exceptions.ValidationError(str(e))
        return _rejected_response(result)


class OtlpMetricsView(views.APIView):
    """OTLP/HTTP metrics receiver (POST /v1/metrics of the OTLP spec)."""

    permission_classes = (permissions.IsAuthenticated,)
    parser_classes = []
    # Shared with the native endpoint: one budget, whichever door is used.
    throttle_scope = "metrics_ingest"

    @extend_schema(
        summary="Receive OTLP/HTTP metrics",
        description=(
            "Accepts an OpenTelemetry ExportMetricsServiceRequest as JSON or "
            "protobuf, optionally gzip-encoded. Each resource must carry the "
            "waldur.resource.uuid attribute; a metric's name is the key of a "
            "metric the resource's offering adopts. Gauge and Sum (delta or "
            "cumulative) are accepted; other types and refused points are "
            "counted in partial_success."
        ),
        request={
            otlp.JSON: OpenApiTypes.OBJECT,
            otlp.PROTOBUF: OpenApiTypes.BINARY,
        },
        responses={status.HTTP_200_OK: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample(
                "Gauge",
                request_only=True,
                value={
                    "resourceMetrics": [
                        {
                            "resource": {
                                "attributes": [
                                    {
                                        "key": "waldur.resource.uuid",
                                        "value": {"stringValue": "<resource UUID>"},
                                    }
                                ]
                            },
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": "education.learners.active",
                                            "gauge": {
                                                "dataPoints": [
                                                    {
                                                        "timeUnixNano": "1790000000000000000",
                                                        "asInt": "42",
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                },
            )
        ],
    )
    def post(self, request):
        content_type = request.content_type or otlp.JSON
        try:
            message = otlp.parse(
                request.body,
                content_type,
                request.headers.get("Content-Encoding", ""),
            )
        except otlp.InvalidPayload as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except otlp.PayloadTooLarge as e:
            return Response(
                {"detail": str(e)}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        points, refused = otlp.to_points(message)
        try:
            result = ingest.ingest(request, points)
        except ingest.Rejected as e:
            return Response(
                {"detail": str(e)}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        rejected = sum(count for count, _reason in refused) + len(result.rejected)
        reasons = [reason for _count, reason in refused] + [
            r["reason"] for r in result.rejected
        ]
        body, media_type = otlp.response(
            rejected, "; ".join(dict.fromkeys(reasons))[:1000], content_type
        )
        return HttpResponse(body, content_type=media_type, status=status.HTTP_200_OK)


def _goal_permission(request, goal_offering_metric, project):
    if request.user.is_staff:
        return
    if project is not None:
        allowed = has_permission(
            request, PermissionEnum.UPDATE_PROJECT, project
        ) or has_permission(request, PermissionEnum.UPDATE_PROJECT, project.customer)
    else:
        allowed = _manages_offering(request, goal_offering_metric.offering)
    if not allowed:
        raise exceptions.PermissionDenied()


class MetricGoalViewSet(core_views.ActionsViewSet):
    """Goals for a metric: the offering's default, or one project's own."""

    queryset = models.MetricGoal.objects.all().select_related(
        "offering_metric__offering", "offering_metric__definition", "project"
    )
    serializer_class = serializers.MetricGoalSerializer
    filterset_class = filters.MetricGoalFilter
    filter_backends = (DjangoFilterBackend,)
    lookup_field = "uuid"

    def check_create_permissions(request, view, obj=None):
        # Resolve only what the permission needs: validating the rest first
        # would tell an outsider whether the project uses the offering.
        offering_metric = (
            models.OfferingMetric.objects.exclude(
                state=enums.OfferingMetricStates.ARCHIVED
            )
            .filter(uuid=request.data.get("offering_metric") or None)
            .first()
        )
        project = None
        if request.data.get("project"):
            project = structure_models.Project.objects.filter(
                uuid=request.data["project"]
            ).first()
            if project is None:
                raise exceptions.PermissionDenied()
        if offering_metric is None:
            raise exceptions.PermissionDenied()
        _goal_permission(request, offering_metric, project)

    def check_object_permissions_for_goal(request, view, obj=None):
        if obj is not None:
            _goal_permission(request, obj.offering_metric, obj.project)

    create_permissions = [check_create_permissions]
    update_permissions = partial_update_permissions = destroy_permissions = [
        check_object_permissions_for_goal
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        qs = qs.filter(offering_metric__in=query.visible_offering_metrics(user))
        return qs.filter(
            Q(project__isnull=True)
            | Q(project__in=get_connected_projects(user))
            | Q(project__customer__in=get_connected_customers(user))
            | Q(offering_metric__offering__customer__in=get_connected_customers(user))
            | Q(
                offering_metric__offering__customer__serviceprovider__in=(
                    query.connected_service_providers(user)
                )
            )
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class MetricSeriesView(views.APIView):
    """A metric's data over time, for charts."""

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        summary="Get metric series",
        parameters=[
            OpenApiParameter(
                "offering_metric_uuid",
                OpenApiTypes.UUID,
                required=True,
                extensions={
                    "x-waldur-operation-id": "marketplace_offering_metrics_retrieve"
                },
            ),
            OpenApiParameter(
                "resource_uuid",
                OpenApiTypes.UUID,
                extensions={"x-waldur-operation-id": "marketplace_resources_retrieve"},
            ),
            OpenApiParameter(
                "project_uuid",
                OpenApiTypes.UUID,
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter("start", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("end", OpenApiTypes.DATETIME),
            OpenApiParameter("granularity", str, enum=["auto", "raw", "hour", "day"]),
            OpenApiParameter(
                "group_by", str, description="Attribute key to break the figure down by"
            ),
            OpenApiParameter(
                "aggregate",
                str,
                enum=["mean", "last", "min", "max"],
                description="How a gauge's points in a bucket combine; counters sum.",
            ),
        ],
        responses=serializers.MetricSeriesResponseSerializer,
    )
    def get(self, request):
        params = serializers.MetricSeriesQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        offering_metric = (
            query.visible_offering_metrics(request.user)
            .filter(uuid=data["offering_metric_uuid"])
            .select_related("definition", "offering")
            .first()
        )
        if offering_metric is None:
            raise exceptions.NotFound()
        resource = project = None
        if data.get("resource_uuid"):
            resource = marketplace_models.Resource.objects.filter(
                uuid=data["resource_uuid"]
            ).first()
            if resource is None:
                raise exceptions.NotFound()
        if data.get("project_uuid"):
            project = structure_models.Project.objects.filter(
                uuid=data["project_uuid"]
            ).first()
            if project is None:
                raise exceptions.NotFound()
        group_by = data.get("group_by")
        if group_by and group_by not in (
            offering_metric.definition.attribute_keys or []
        ):
            raise exceptions.ValidationError(
                {"group_by": _("The metric declares no such attribute.")}
            )
        result = query.series_data(
            request.user,
            offering_metric,
            data["start"],
            data["end"],
            granularity=data["granularity"],
            resource=resource,
            project=project,
            group_by=group_by,
            aggregate=data["aggregate"],
        )
        return Response(serializers.MetricSeriesResponseSerializer(result).data)


class ProjectMetricsView(views.APIView):
    """Each metric a project's resources report, with its figure and goal."""

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        summary="Get a project's metric figures",
        parameters=[
            OpenApiParameter(
                "project_uuid",
                OpenApiTypes.UUID,
                required=True,
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
        ],
        responses=serializers.ProjectMetricSerializer(many=True),
    )
    def get(self, request):
        user = request.user
        projects = structure_models.Project.objects.filter(
            uuid=request.query_params.get("project_uuid") or None
        )
        if not (user.is_staff or user.is_support):
            projects = projects.filter(
                Q(id__in=get_connected_projects(user))
                | Q(customer__in=get_connected_customers(user))
            )
        project = projects.first()
        if project is None:
            raise exceptions.NotFound()
        summary = query.project_summary(user, project)
        return Response(serializers.ProjectMetricSerializer(summary, many=True).data)


class MetricBreakdownView(views.APIView):
    """A figure for a metric, per value of one of its attributes or per resource.

    The figure is a project's or, with resource_uuid, one resource's.
    """

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        summary="Break a metric figure down by an attribute or by resource",
        parameters=[
            OpenApiParameter(
                "offering_metric_uuid",
                OpenApiTypes.UUID,
                required=True,
                extensions={
                    "x-waldur-operation-id": "marketplace_offering_metrics_retrieve"
                },
            ),
            OpenApiParameter(
                "project_uuid",
                OpenApiTypes.UUID,
                description="The project whose figure to break down. Either this or resource_uuid.",
                extensions={"x-waldur-operation-id": "projects_retrieve"},
            ),
            OpenApiParameter(
                "resource_uuid",
                OpenApiTypes.UUID,
                description="The resource whose figure to break down. Either this or project_uuid.",
                extensions={"x-waldur-operation-id": "marketplace_resources_retrieve"},
            ),
            OpenApiParameter(
                "group_by",
                str,
                required=True,
                description=(
                    "An attribute the metric declares, or 'resource' to break a "
                    "project's figure down by its resources."
                ),
            ),
            OpenApiParameter("start", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("end", OpenApiTypes.DATETIME),
        ],
        responses=serializers.MetricBreakdownItemSerializer(many=True),
    )
    def get(self, request):
        params = request.query_params
        user = request.user
        offering_metric = (
            query.visible_offering_metrics(user)
            .filter(uuid=params.get("offering_metric_uuid") or None)
            .select_related("definition", "offering")
            .first()
        )
        project = resource = None
        if params.get("resource_uuid"):
            resource = query.visible_resource(user, params["resource_uuid"])
            if resource is None or (
                offering_metric and resource.offering_id != offering_metric.offering_id
            ):
                raise exceptions.NotFound()
        else:
            projects = structure_models.Project.objects.filter(
                uuid=params.get("project_uuid") or None
            )
            if not (user.is_staff or user.is_support):
                projects = projects.filter(
                    Q(id__in=get_connected_projects(user))
                    | Q(customer__in=get_connected_customers(user))
                )
            project = projects.first()
            if project is None:
                raise exceptions.NotFound()
        if offering_metric is None:
            raise exceptions.NotFound()
        group_by = params.get("group_by")
        if group_by == query.GROUP_BY_RESOURCE:
            if resource is not None:
                raise exceptions.ValidationError(
                    {
                        "group_by": _(
                            "A resource's figure cannot be broken down by resource."
                        )
                    }
                )
        elif group_by not in (offering_metric.definition.attribute_keys or []):
            raise exceptions.ValidationError(
                {"group_by": _("The metric declares no such attribute.")}
            )
        window = serializers.MetricSeriesQuerySerializer(
            data={
                "offering_metric_uuid": offering_metric.uuid.hex,
                "start": params.get("start"),
                **({"end": params["end"]} if params.get("end") else {}),
            }
        )
        window.is_valid(raise_exception=True)
        result = query.breakdown(
            user,
            offering_metric,
            group_by,
            window.validated_data["start"],
            window.validated_data["end"],
            project=project,
            resource=resource,
        )
        return Response(
            serializers.MetricBreakdownItemSerializer(result, many=True).data
        )


class ResourceMetricsView(views.APIView):
    """Each metric a resource reports, with this month's and last month's figure."""

    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        summary="Get a resource's metric figures",
        description=(
            "Figures for the current and the previous calendar month. A resource "
            "has no goals: they apply to a project's combined figure."
        ),
        parameters=[
            OpenApiParameter(
                "resource_uuid",
                OpenApiTypes.UUID,
                required=True,
                extensions={"x-waldur-operation-id": "marketplace_resources_retrieve"},
            ),
        ],
        responses=serializers.ResourceMetricSerializer(many=True),
    )
    def get(self, request):
        resource = query.visible_resource(
            request.user, request.query_params.get("resource_uuid")
        )
        if resource is None:
            raise exceptions.NotFound()
        summary = query.resource_summary(request.user, resource)
        return Response(serializers.ResourceMetricSerializer(summary, many=True).data)
