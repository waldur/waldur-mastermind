from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from httpx import TimeoutException
from rest_framework import permissions as rf_permissions
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from waldur_api_client.api.customers import customers_list
from waldur_api_client.api.marketplace_categories import marketplace_categories_list
from waldur_api_client.api.marketplace_orders import (
    marketplace_orders_reject_by_consumer,
)
from waldur_api_client.api.marketplace_public_offerings import (
    marketplace_public_offerings_retrieve,
)
from waldur_api_client.errors import UnexpectedStatus
from waldur_api_client.models.customers_list_field_item import CustomersListFieldItem
from waldur_api_client.models.marketplace_public_offerings_list_field_item import (
    MarketplacePublicOfferingsListFieldItem,
)

from waldur_core.core import permissions as core_permissions
from waldur_core.core import views as core_views
from waldur_core.core.client import get_waldur_client
from waldur_core.core.enums import ReviewStates
from waldur_core.core.serializers import EmptySerializer, ReviewCommentSerializer
from waldur_core.core.utils import is_uuid_like, serialize_instance
from waldur_core.core.validators import StateValidator
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ServiceProviderRole
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import filters as structure_filters
from waldur_core.structure.filters import GenericRoleFilter
from waldur_core.structure.models import Customer
from waldur_core.structure.permissions import _has_owner_access
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.serializers import MarketplaceCategorySerializer
from waldur_mastermind.marketplace_remote import (
    PLUGIN_NAME,
    filters,
    serializers,
    tasks,
    utils,
    utils_sync_remote_offerings,
)
from waldur_mastermind.marketplace_remote.models import (
    ProjectUpdateRequest,
    RemoteSynchronisation,
)


class RemoteView(GenericAPIView):
    """View for handling remote credentials for waldur client"""

    serializer_class = serializers.RemoteCredentialsSerializer
    filter_backends = []

    def get_client(self, request):
        serializer = serializers.RemoteCredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        api_url = serializer.validated_data["api_url"]
        token = serializer.validated_data["token"]
        client = get_waldur_client(api_url, token)
        return client


class CustomersView(RemoteView):
    @extend_schema(
        request=serializers.RemoteCredentialsSerializer,
        responses=serializers.RemoteCustomerSerializer(many=True),
        description="List remote customers owned by current user",
    )
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        try:
            customers = customers_list.sync(
                client=client,
                owned_by_current_user=True,
                field=[
                    CustomersListFieldItem.UUID,
                    CustomersListFieldItem.NAME,
                    CustomersListFieldItem.ABBREVIATION,
                    CustomersListFieldItem.PHONE_NUMBER,
                    CustomersListFieldItem.EMAIL,
                ],
            )
        except (UnexpectedStatus, TimeoutException) as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        return Response([customer.to_dict() for customer in customers])


class СategoriesView(RemoteView):
    @extend_schema(
        request=serializers.RemoteCredentialsSerializer,
        responses=MarketplaceCategorySerializer(many=True),
        description="List remote marketplace categories",
    )
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        try:
            сategories = marketplace_categories_list.sync(client=client)
        except (UnexpectedStatus, TimeoutException) as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        return Response([category.to_dict() for category in сategories])


class OfferingsListView(RemoteView):
    @extend_schema(
        request=serializers.RemoteCredentialsSerializer,
        responses=serializers.RemoteOfferingSerializer(many=True),
        parameters=[
            OpenApiParameter(
                name="customer_uuid", type=str, location=OpenApiParameter.QUERY
            )
        ],
        description="List remote importable offerings for particular customer",
    )
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        if "customer_uuid" not in request.query_params:
            raise ValidationError(
                {"url": _("customer_uuid field must be present in query parameters")}
            )

        remote_customer_uuid = request.query_params["customer_uuid"]
        try:
            remote_offerings = utils.get_remote_offerings(
                client,
                remote_customer_uuid,
                fields=[
                    MarketplacePublicOfferingsListFieldItem.UUID,
                    MarketplacePublicOfferingsListFieldItem.NAME,
                    MarketplacePublicOfferingsListFieldItem.TYPE,
                    MarketplacePublicOfferingsListFieldItem.STATE,
                    MarketplacePublicOfferingsListFieldItem.CATEGORY_TITLE,
                ],
            )
        except (UnexpectedStatus, TimeoutException) as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        local_offerings = list(
            models.Offering.objects.filter(type=PLUGIN_NAME)
            .exclude(state=OfferingStates.ARCHIVED)
            .values_list("backend_id", flat=True)
        )

        importable_offerings = [
            offering
            for offering in remote_offerings
            if offering.uuid.hex not in local_offerings
        ]
        return Response(importable_offerings)


class OfferingCreateView(RemoteView):
    @extend_schema(
        request=serializers.RemoteOfferingCreateSerializer,
        responses=serializers.RemoteOfferingCreateResponseSerializer,
        description="Create local offering from remote",
    )
    def post(self, request, *args, **kwargs):
        serializer = serializers.RemoteOfferingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client = self.get_client(request)

        api_url = serializer.validated_data["api_url"]
        token = serializer.validated_data["token"]
        remote_offering_uuid = serializer.validated_data["remote_offering_uuid"]
        remote_customer_uuid = serializer.validated_data["remote_customer_uuid"]
        local_customer_uuid = serializer.validated_data["local_customer_uuid"]
        local_category_uuid = serializer.validated_data["local_category_uuid"]

        local_customer = Customer.objects.get(uuid=local_customer_uuid)
        local_category = models.Category.objects.get(uuid=local_category_uuid)

        if not has_permission(request, PermissionEnum.CREATE_OFFERING, local_customer):
            raise PermissionDenied()

        try:
            remote_offering = marketplace_public_offerings_retrieve.sync(
                client=client, uuid=remote_offering_uuid.hex
            )
        except (UnexpectedStatus, TimeoutException) as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        secret_options = {
            "api_url": api_url,
            "token": token,
            "customer_uuid": remote_customer_uuid.hex,
        }
        local_offering = utils.upsert_offering(
            remote_offering=remote_offering,
            local_category=local_category,
            secret_options=secret_options,
            local_customer=local_customer,
        )

        return Response({"uuid": local_offering.uuid.hex})


def user_is_service_provider_owner_or_service_provider_manager(
    request, view, obj: ProjectUpdateRequest | None = None
):
    if not obj:
        return

    if _has_owner_access(request.user, obj.offering.customer):
        return

    if obj.offering.customer.has_user(request.user, role=ServiceProviderRole.MANAGER):
        return

    raise PermissionDenied()


class ProjectUpdateRequestViewSet(ActionsViewSet):
    queryset = ProjectUpdateRequest.objects.all()
    approve_permissions = reject_permissions = [
        user_is_service_provider_owner_or_service_provider_manager
    ]
    serializer_class = serializers.RemoteProjectUpdateRequestSerializer
    filter_backends = [GenericRoleFilter, DjangoFilterBackend]
    filterset_class = filters.ProjectUpdateRequestFilter

    disabled_actions = ["create", "destroy", "update", "partial_update"]
    lookup_field = "uuid"

    @extend_schema(
        request=ReviewCommentSerializer,
        responses=None,
        description="Approve project update request",
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, **kwargs):
        review_request: ProjectUpdateRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        review_request.approve(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        request=ReviewCommentSerializer,
        responses=None,
        description="Reject project update request",
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, **kwargs):
        review_request: ProjectUpdateRequest = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        review_request.reject(request.user, comment)
        return Response(status=status.HTTP_200_OK)

    approve_serializer_class = reject_serializer_class = ReviewCommentSerializer
    approve_validators = reject_validators = [
        StateValidator(ReviewStates.PENDING, state_enum=ReviewStates)
    ]


class PullOrderView(GenericAPIView):
    permission_classes = []
    filter_backends = []
    serializer_class = EmptySerializer

    def get_order(self):
        item_uuid = self.kwargs["uuid"]
        if not is_uuid_like(item_uuid):
            return Response(status=status.HTTP_400_BAD_REQUEST, data="UUID is invalid.")
        qs = models.Order.objects.filter(offering__type=PLUGIN_NAME).exclude(
            state__in=OrderStates.TERMINAL_STATES
        )
        return get_object_or_404(qs, uuid=item_uuid)

    @extend_schema(description="Schedule order pull task")
    def post(self, *args, **kwargs):
        order = self.get_order()
        tasks.OrderPullTask.apply_async(args=[serialize_instance(order)])
        return Response(status=status.HTTP_200_OK)


class CancelTerminationOrderView(GenericAPIView):
    serializer_class = EmptySerializer
    filter_backends = []

    def get_order(self):
        item_uuid = self.kwargs["uuid"]
        if not is_uuid_like(item_uuid):
            raise ValidationError("UUID is invalid.")
        qs = models.Order.objects.filter(
            offering__type=PLUGIN_NAME,
            state=OrderStates.EXECUTING,
            type=models.Order.Types.TERMINATE,
        )
        return get_object_or_404(qs, uuid=item_uuid)

    @extend_schema(description="Cancel termination order")
    def post(self, request, *args, **kwargs):
        order = self.get_order()
        if not has_permission(
            request, PermissionEnum.APPROVE_ORDER, order.offering.customer
        ):
            raise PermissionDenied()

        client = utils.get_client_for_offering(order.resource.offering)

        try:
            marketplace_orders_reject_by_consumer.sync_detailed(
                client=client, uuid=order.backend_id
            )
        except (UnexpectedStatus, TimeoutException) as exc:
            raise ValidationError(exc)
        callbacks.sync_order_state(order, OrderStates.CANCELED)

        return Response(status=status.HTTP_200_OK)


class OfferingActionView(GenericAPIView):
    serializer_class = EmptySerializer
    filter_backends = []

    def post(self, request, uuid):
        qs = models.Offering.objects.filter(type=PLUGIN_NAME)
        offering = get_object_or_404(qs, uuid=uuid)
        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING, offering
        ) and not has_permission(
            request, PermissionEnum.UPDATE_OFFERING, offering.customer
        ):
            raise PermissionDenied()
        self.task.delay(serialize_instance(offering))
        return Response(status=status.HTTP_200_OK)


class PullOfferingDetails(OfferingActionView):
    task = tasks.OfferingPullTask()


class PullOfferingUsers(OfferingActionView):
    task = tasks.OfferingUserPullTask()


class PullOfferingResources(OfferingActionView):
    task = tasks.pull_offering_resources


class PullOfferingOrders(OfferingActionView):
    task = tasks.pull_offering_orders


class PullOfferingUsage(OfferingActionView):
    task = tasks.pull_offering_usage


class PullOfferingInvoices(OfferingActionView):
    task = tasks.pull_offering_invoices


class PullOfferingRobotAccounts(OfferingActionView):
    task = tasks.pull_offering_robot_accounts


class PushProjectData(OfferingActionView):
    task = tasks.RemoteProjectDataPushTask()


class SyncResourceProjectPermissions(GenericAPIView):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]

    serializer_class = EmptySerializer

    def post(self, request, uuid):
        qs = models.Resource.objects.filter(offering__type=PLUGIN_NAME)
        resource = get_object_or_404(qs, uuid=uuid)
        utils.sync_resource_team(resource)
        return Response(status=status.HTTP_200_OK)


class RemoteSynchronisationViewSet(core_views.ActionsViewSet):
    queryset = RemoteSynchronisation.objects.all().order_by("-created")
    lookup_field = "uuid"
    serializer_class = serializers.RemoteSynchronisationSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def run_synchronisation(self, request, **kwargs):
        sync: RemoteSynchronisation = self.get_object()
        utils_sync_remote_offerings.RemoteSynchronisationRunner(sync).run()
        sync.refresh_from_db()
        return Response(
            serializers.RemoteSynchronisationSerializer(
                instance=sync, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_200_OK,
        )


class SyncResourceView(GenericAPIView):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]

    def get_resource(self):
        resource_uuid = self.kwargs["uuid"]
        if not is_uuid_like(resource_uuid):
            return Response(status=status.HTTP_400_BAD_REQUEST, data="UUID is invalid.")
        resource = models.Resource.objects.filter(uuid=resource_uuid).first()
        if resource is None:
            return Response(
                status=status.HTTP_404_NOT_FOUND, data="A resource is not found"
            )
        if resource.state == ResourceStates.TERMINATED:
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data="The resource is terminated"
            )
        if resource.state == ResourceStates.UPDATING:
            return Response(
                status=status.HTTP_400_BAD_REQUEST, data="The resource is updating"
            )
        return resource

    serializer_class = EmptySerializer

    def post(self, *args, **kwargs):
        resource = self.get_resource()
        tasks.ResourcePullTask.apply_async(args=[serialize_instance(resource)])
        return Response(status=status.HTTP_200_OK)
