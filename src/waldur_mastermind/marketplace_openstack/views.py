import datetime
import logging

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import exceptions as rf_exceptions
from rest_framework import response, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied

from waldur_core.core import views as core_views
from waldur_core.core.mixins import ExecutorMixin
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import filters
from waldur_core.structure.permissions import (
    is_administrator,
    is_staff_or_support,
)
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
)
from waldur_mastermind.marketplace_metrics import query as metrics_query
from waldur_mastermind.marketplace_openstack import (
    metrics,
    models,
    serializers,
    tasks,
    utils,
)
from waldur_openstack import models as openstack_models
from waldur_openstack import permissions as openstack_permissions
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.executors import TenantCreateExecutor, TenantDeleteExecutor

logger = logging.getLogger(__name__)


class MarketplaceTenantViewSet(ExecutorMixin, core_views.ActionsViewSet):
    queryset = openstack_models.Tenant.objects.all().order_by("name")
    lookup_field = "uuid"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        skip_connection_extnet = serializer.validated_data["skip_connection_extnet"]
        skip_creation_of_default_router = serializer.validated_data[
            "skip_creation_of_default_router"
        ]
        TenantCreateExecutor.execute(
            tenant,
            skip_connection_extnet=skip_connection_extnet,
            skip_creation_of_default_router=skip_creation_of_default_router,
        )

        return response.Response(
            {"uuid": tenant.uuid.hex}, status=status.HTTP_201_CREATED
        )

    serializer_class = serializers.MarketplaceTenantCreateSerializer

    def delete_permission_check(request, view, obj=None):
        if not obj:
            return
        if obj.service_settings.shared:
            if has_permission(
                request, PermissionEnum.APPROVE_ORDER, obj.project
            ) or has_permission(
                request, PermissionEnum.APPROVE_ORDER, obj.project.customer
            ):
                return
            raise PermissionDenied()
        else:
            is_administrator(
                request,
                view,
                obj,
            )

    delete_executor = TenantDeleteExecutor
    destroy_permissions = [delete_permission_check]


class MarketplaceTenantActionsViewSet(core_views.ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    filter_backends = (filters.GenericRoleFilter, DjangoFilterBackend)

    queryset = openstack_models.Tenant.objects.all().order_by("name")
    serializer_class = serializers.TenantSerializer
    filterset_class = filters.BaseResourceFilter

    @extend_schema(
        request=serializers.ImageCreateSerializer,
        responses={201: serializers.ImageCreateResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def create_image(self, request, uuid=None):
        tenant = self.get_object()
        backend = tenant.get_backend()

        offering = marketplace_models.Offering.objects.filter(scope=tenant).first()
        if not offering:
            raise ValidationError(
                _(
                    "Operation is not possible: no related offering found for this tenant."
                )
            )
        # public_offering is used because it is associated with the admin tenant.
        # We need to check if the user has the required permissions relative to the admin tenant.
        public_offering = offering.parent or offering
        public_images_is_available = permissions_utils.has_permission(
            request,
            PermissionEnum.SERVICE_PROVIDER_OPENSTACK_IMAGE_MANAGEMENT,
            public_offering.customer,
        )

        serializer = self.get_serializer(
            data=request.data,
            context={"public_images_is_available": public_images_is_available},
        )
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data
        image_count_total_limit = public_offering.attributes.get(
            "image_count_total_limit"
        )

        if image_count_total_limit:
            image_count_total = backend.get_image_count_total()

            if image_count_total >= image_count_total_limit:
                raise ValidationError(
                    _(
                        "Image count limit exceeded. Current: {image_count_total}, Limit: {image_count_total_limit}"
                    ),
                    params={
                        "image_count_total": image_count_total,
                        "image_count_total_limit": image_count_total_limit,
                    },
                )

        image_metadata = {
            "name": validated_data["name"],
            "disk_format": validated_data["disk_format"],
            "container_format": validated_data["container_format"],
            "visibility": validated_data["visibility"],
            "min_disk": validated_data["min_disk"],
            "min_ram": validated_data["min_ram"],
        }

        try:
            image = backend.create_image(tenant, image_metadata)

            return response.Response(
                {
                    "image_id": image["id"],
                    "name": image["name"],
                    "status": image["status"],
                    "upload_url": reverse(
                        "openstack-marketplace-upload-image-data",
                        kwargs={"uuid": tenant.uuid, "image_id": image["id"]},
                    ),
                },
                status=status.HTTP_201_CREATED,
            )

        except OpenStackBackendError as e:
            return response.Response(
                {"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

    create_image_serializer_class = serializers.ImageCreateSerializer

    @extend_schema(
        request=OpenApiTypes.BINARY,
        responses={200: serializers.ImageUploadResponseSerializer},
        parameters=[
            OpenApiParameter(
                name="image_id",
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.PATH,
            ),
        ],
    )
    @action(
        detail=True,
        methods=["post"],
        url_path=r"upload_image_data/(?P<image_id>[0-9a-fA-F-]{36})",
    )
    def upload_image_data(self, request, uuid=None, image_id=None):
        if not image_id:
            return response.Response(
                {"detail": "Image ID is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        tenant = self.get_object()
        backend = tenant.get_backend()

        # Check image size limit
        offering = marketplace_models.Offering.objects.filter(scope=tenant).first()
        if not offering:
            raise ValidationError(
                _(
                    "Operation is not possible: no related offering found for this tenant."
                )
            )

        # public_offering is used because it is associated with the admin tenant.
        # We need to check if the user has the required permissions relative to the admin tenant.
        public_offering = offering.parent or offering
        image_size_total_limit = public_offering.attributes.get(
            "image_size_total_limit"
        )

        if image_size_total_limit:
            # Get file size from request
            content_length = request.META.get("CONTENT_LENGTH")
            if content_length:
                file_size = int(content_length)
                image_size_total = backend.get_image_size_total()

                if image_size_total + file_size > image_size_total_limit:
                    raise ValidationError(
                        _(
                            "Image size limit would be exceeded. "
                            "Current total: %(image_size_total)s, "
                            "File size: %(file_size), "
                            "Limit: %(image_size_total_limit)"
                        ),
                        params={
                            "image_size_total": image_size_total,
                            "file_size": file_size,
                            "image_size_total_limit": image_size_total_limit,
                        },
                    )

        try:
            updated_image = backend.upload_image_data(tenant, image_id, request.stream)

            return response.Response(
                {
                    "response": updated_image["response"],
                    "status": updated_image["status"],
                },
                status=status.HTTP_200_OK,
            )

        except OpenStackBackendError as e:
            return response.Response(
                {"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )


class DuplicateTenantOfferingViewSet(core_views.ReadOnlyActionsViewSet):
    """Staff/support read-only diagnostics for duplicate per-tenant offerings.

    Surfaces tenants that have more than one per-tenant OpenStack.Instance or
    OpenStack.Volume offering — the state that makes self-heal give up and
    leaves VMs/volumes orphaned from the marketplace. Read-only: each group is
    resolved by an offering merge of its duplicates into the keeper, created
    and run through ``/api/marketplace-offering-merges/`` with the UUIDs and
    suggested mapping this list returns.
    """

    # The report is computed in-memory (a scan across offerings), not a plain
    # queryset; list() overrides the default. `queryset` only satisfies the
    # router/schema machinery. There is no per-object detail view.
    queryset = marketplace_models.Offering.objects.none()
    serializer_class = serializers.DuplicateOfferingGroupSerializer
    filter_backends = ()
    disabled_actions = ["create", "update", "partial_update", "destroy", "retrieve"]
    list_permissions = [is_staff_or_support]

    @extend_schema(
        responses={200: serializers.DuplicateOfferingGroupSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        report = utils.build_duplicate_offering_report()
        page = self.paginate_queryset(report)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(report, many=True)
        return response.Response(serializer.data)


@extend_schema(
    # The resource is looked up by hand, so the schema can't read the type.
    parameters=[
        OpenApiParameter(
            name="uuid",
            type=OpenApiTypes.UUID,
            location=OpenApiParameter.PATH,
            description="UUID of the instance or volume resource.",
        ),
    ],
)
class InstanceMetricsViewSet(viewsets.GenericViewSet):
    """How often Waldur reads an instance's metrics from Nova, and asking for more.

    The provider's interval is slow, to spare the Nova API and storage. Someone
    who manages the instance may have it read faster for a while, or once now.
    A volume is read with the instance it is attached to, so asking on a
    volume acts on that instance.
    """

    lookup_field = "uuid"
    serializer_class = serializers.InstanceMetricsPollingSerializer

    def get_object(self):
        resource = metrics_query.visible_resource(
            self.request.user, self.kwargs["uuid"]
        )
        if resource is None or resource.offering.type not in (
            OPENSTACK_INSTANCE_OFFERING,
            OPENSTACK_VOLUME_OFFERING,
        ):
            raise NotFound()
        return resource

    def _instance_resource(self, resource):
        """The instance whose polls read the resource; None for a detached volume."""
        scope = resource.scope
        if resource.offering.type == OPENSTACK_INSTANCE_OFFERING:
            return resource if isinstance(scope, openstack_models.Instance) else None
        instance = getattr(scope, "instance", None)
        if instance is None:
            return None
        return (
            marketplace_models.Resource.objects.filter(scope=instance)
            .exclude(state=marketplace_models.Resource.States.TERMINATED)
            .select_related("offering__parent")
            .first()
        )

    def _can_manage(self, instance):
        try:
            openstack_permissions.can_manage_openstack_instance(
                self.request, self, instance
            )
        except PermissionDenied:
            return False
        return True

    def _status(self, resource):
        now = timezone.now()
        enabled = metrics.is_enabled(resource.offering)
        offering_interval = metrics.poll_interval(resource.offering)
        reader = self._instance_resource(resource)
        data = {
            "enabled": enabled,
            "interval_minutes": offering_interval,
            "offering_interval_minutes": offering_interval,
            "faster_until": None,
            "last_polled_at": None,
            "next_poll_at": None,
            "can_manage": False,
            "instance_name": None,
            "instance_resource_uuid": None,
        }
        if reader is not None:
            instance = reader.scope
            schedule = models.InstanceMetricSchedule.objects.filter(
                instance=instance, until__gt=now
            ).first()
            interval = metrics.current_interval(offering_interval, schedule, now)
            sample = models.InstanceMetricSample.objects.filter(
                instance=instance
            ).first()
            running = (
                instance.runtime_state == openstack_models.Instance.RuntimeStates.ACTIVE
            )
            data.update(
                {
                    "interval_minutes": interval,
                    "faster_until": schedule.until if schedule else None,
                    "last_polled_at": sample.sampled_at if sample else None,
                    "next_poll_at": (
                        metrics.next_poll(reader, interval, now)
                        if enabled and running
                        else None
                    ),
                    "can_manage": self._can_manage(instance),
                    "instance_name": reader.name,
                    "instance_resource_uuid": reader.uuid,
                }
            )
        return response.Response(
            serializers.InstanceMetricsPollingSerializer(data).data
        )

    def _managed_reader(self):
        """The resource asked about and the instance resource that reads it."""
        resource = self.get_object()
        reader = self._instance_resource(resource)
        if reader is None:
            raise rf_exceptions.ValidationError(
                _("The volume is not attached to an instance, so it is not read.")
                if resource.offering.type == OPENSTACK_VOLUME_OFFERING
                else _("The instance has no OpenStack server, so it is not read.")
            )
        if not self._can_manage(reader.scope):
            raise PermissionDenied()
        if not metrics.is_enabled(resource.offering):
            raise rf_exceptions.ValidationError(
                _("The provider does not collect metrics for this resource.")
            )
        return resource, reader

    def _require_running(self, reader):
        """Only a running server can be read; Nova refuses the others."""
        if reader.scope.runtime_state != openstack_models.Instance.RuntimeStates.ACTIVE:
            raise rf_exceptions.ValidationError(
                _("The instance is not running, so it can't be read.")
            )

    def _poll_soon(self, reader):
        """Read the instance soon, unless someone asked for that a moment ago.

        The cache key is taken at once, so requests that come together, before
        the first read is recorded, queue one read between them.
        """
        key = f"marketplace-openstack:instance-metrics:requested:{reader.scope.id}"
        if not cache.add(key, True, timeout=metrics.REQUEST_SECONDS):
            return False
        transaction.on_commit(
            lambda: tasks.collect_resource_metrics.apply_async(
                args=(reader.id,),
                expires=metrics.poll_expiry(metrics.MIN_POLL_INTERVAL),
            )
        )
        return True

    @extend_schema(summary="Get how often an instance's or volume's metrics are read")
    def retrieve(self, request, uuid=None):
        return self._status(self.get_object())

    @extend_schema(
        summary="Read an instance's metrics faster for a while",
        description=(
            "Reads the instance now, then every interval_minutes for the given "
            "hours, after which the provider's interval applies again. Asked on "
            "a volume, it applies to the instance the volume is attached to."
        ),
        request=serializers.InstanceMetricsFasterUpdatesSerializer,
        responses=serializers.InstanceMetricsPollingSerializer,
    )
    @action(detail=True, methods=["post"])
    def faster_updates(self, request, uuid=None):
        resource, reader = self._managed_reader()
        self._require_running(reader)
        serializer = serializers.InstanceMetricsFasterUpdatesSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        # A slower interval than the provider's would change nothing.
        offering_interval = metrics.poll_interval(resource.offering)
        if serializer.validated_data["interval_minutes"] >= offering_interval:
            raise rf_exceptions.ValidationError(
                {
                    "interval_minutes": _(
                        "Choose an interval shorter than the provider's, "
                        "%(minutes)s minutes."
                    )
                    % {"minutes": offering_interval}
                }
            )
        models.InstanceMetricSchedule.objects.update_or_create(
            instance=reader.scope,
            defaults={
                "interval": serializer.validated_data["interval_minutes"],
                "until": timezone.now()
                + datetime.timedelta(hours=serializer.validated_data["hours"]),
                "requested_by": request.user,
            },
        )
        self._poll_soon(reader)
        return self._status(resource)

    @extend_schema(
        summary="Return an instance to the provider's interval",
        request=None,
        responses=serializers.InstanceMetricsPollingSerializer,
    )
    @action(detail=True, methods=["post"])
    def normal_updates(self, request, uuid=None):
        resource, reader = self._managed_reader()
        models.InstanceMetricSchedule.objects.filter(instance=reader.scope).delete()
        return self._status(resource)

    @extend_schema(
        summary="Read an instance's metrics now",
        description=(
            "Refused while the last read, or the last one asked for, is less "
            "than five minutes old."
        ),
        request=None,
        responses=serializers.InstanceMetricsPollingSerializer,
    )
    @action(detail=True, methods=["post"])
    def update_now(self, request, uuid=None):
        resource, reader = self._managed_reader()
        self._require_running(reader)
        recent = timezone.now() - datetime.timedelta(minutes=metrics.MIN_POLL_INTERVAL)
        if models.InstanceMetricSample.objects.filter(
            instance=reader.scope, sampled_at__gt=recent
        ).exists() or not self._poll_soon(reader):
            raise rf_exceptions.ValidationError(
                _("The metrics were read, or asked for, less than five minutes ago.")
            )
        return self._status(resource)
