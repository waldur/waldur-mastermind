from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import response, status
from rest_framework.decorators import action

from waldur_core.core import views as core_views
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure import filters
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import serializers
from waldur_openstack import models as openstack_models
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.executors import TenantCreateExecutor


class MarketplaceTenantViewSet(core_views.ActionsViewSet):
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        skip = serializer.validated_data["skip_connection_extnet"]
        TenantCreateExecutor.execute(tenant, skip_connection_extnet=skip)

        return response.Response(
            {"uuid": tenant.uuid.hex}, status=status.HTTP_201_CREATED
        )

    serializer_class = serializers.MarketplaceTenantCreateSerializer


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
