import logging

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import exceptions as rf_exceptions
from rest_framework import response, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied

from waldur_core.core import views as core_views
from waldur_core.core.mixins import ExecutorMixin
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import filters
from waldur_core.structure.permissions import (
    is_administrator,
    is_staff,
    is_staff_or_support,
)
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import serializers, utils
from waldur_openstack import models as openstack_models
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
    leaves VMs/volumes orphaned from the marketplace. Read-only: remediation
    stays in the ``dedupe_tenant_offerings`` management command.
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

    # Reading the report is staff-or-support, but collapsing a group deletes
    # offerings, so it is staff-only.
    remediate_serializer_class = serializers.DuplicateOfferingRemediateSerializer
    remediate_permissions = [is_staff]

    @extend_schema(
        request=serializers.DuplicateOfferingRemediateSerializer,
        responses={200: serializers.DuplicateOfferingRemediationSerializer},
        description="Collapse one duplicate per-tenant offering group onto its "
        "keeper. Previews by default; pass dry_run=false to apply. Refuses to "
        "run when history (billing periods, usage records) cannot be re-pointed.",
    )
    @action(detail=False, methods=["post"])
    def remediate(self, request, *args, **kwargs):
        # detail=False: the queryset is Offering.objects.none() and retrieve is
        # disabled, so there is no detail route to hang this on. The group is
        # addressed by (tenant_id, offering_type) instead.
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = utils.remediate_duplicate_offering_group(
                tenant_id=serializer.validated_data["tenant_id"],
                offering_type=serializer.validated_data["offering_type"],
                dry_run=serializer.validated_data["dry_run"],
            )
        except utils.OfferingMergeBlocked as e:
            raise rf_exceptions.ValidationError(e.blockers)

        if not result["dry_run"]:
            logger.info(
                "Staff user %s remediated duplicate %s offerings for tenant %s "
                "onto keeper id=%s.",
                request.user.username,
                result["offering_type"],
                result["tenant_id"],
                result["keeper_id"],
            )
        return response.Response(
            serializers.DuplicateOfferingRemediationSerializer(result).data,
            status=status.HTTP_200_OK,
        )
