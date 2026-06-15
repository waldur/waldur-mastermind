from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from httpx import TimeoutException
from rest_framework import exceptions, status
from rest_framework import permissions as rf_permissions
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
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
from waldur_api_client.models.customer_field_enum import CustomerFieldEnum
from waldur_api_client.models.public_offering_details_field_enum import (
    PublicOfferingDetailsFieldEnum,
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
from waldur_core.structure.managers import (
    get_connected_customers_by_permission,
    get_connected_projects_by_permission,
)
from waldur_core.structure.models import Customer
from waldur_core.structure.permissions import _has_owner_access
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.serializers import MarketplaceCategorySerializer
from waldur_mastermind.marketplace_remote import (
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
from waldur_mastermind.marketplace_remote.utils import (
    get_resource_order_sync_status,
    get_resource_sync_status,
    get_resource_team,
)


def check_remote_resource_access(request: Request, resource: models.Resource):
    """Check if user has access to remote resource"""
    if request.user.is_staff or request.user.is_support:
        return

    connected_projects = get_connected_projects_by_permission(
        request.user, PermissionEnum.LIST_RESOURCES
    )
    connected_customers = get_connected_customers_by_permission(
        request.user, PermissionEnum.LIST_RESOURCES
    )

    if not (
        resource.project.id in connected_projects
        or resource.project.customer.id in connected_customers
    ):
        raise NotFound()


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
            customers = customers_list.sync_all(
                client=client,
                owned_by_current_user=True,
                field=[
                    CustomerFieldEnum.UUID,
                    CustomerFieldEnum.NAME,
                    CustomerFieldEnum.ABBREVIATION,
                    CustomerFieldEnum.PHONE_NUMBER,
                    CustomerFieldEnum.EMAIL,
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
            сategories = marketplace_categories_list.sync_all(client=client)
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
                    PublicOfferingDetailsFieldEnum.UUID,
                    PublicOfferingDetailsFieldEnum.NAME,
                    PublicOfferingDetailsFieldEnum.TYPE,
                    PublicOfferingDetailsFieldEnum.STATE,
                    PublicOfferingDetailsFieldEnum.CATEGORY_TITLE,
                ],
            )
        except (UnexpectedStatus, TimeoutException) as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        local_offerings = list(
            models.Offering.objects.filter(type=REMOTE_OFFERING)
            .exclude(state=OfferingStates.ARCHIVED)
            .values_list("backend_id", flat=True)
        )

        importable_offerings = [
            offering.to_dict()
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
        qs = models.Order.objects.filter(offering__type=REMOTE_OFFERING).exclude(
            state__in=OrderStates.TERMINAL_STATES
        )
        return get_object_or_404(qs, uuid=item_uuid)

    @extend_schema(description="Schedule order pull task")
    def post(self, *args, **kwargs):
        order = self.get_order()
        tasks.OrderPullTask().apply_async(args=[serialize_instance(order)], kwargs={})
        return Response(status=status.HTTP_200_OK)


class CancelTerminationOrderView(GenericAPIView):
    serializer_class = EmptySerializer
    filter_backends = []

    def get_order(self):
        item_uuid = self.kwargs["uuid"]
        if not is_uuid_like(item_uuid):
            raise ValidationError("UUID is invalid.")
        qs = models.Order.objects.filter(
            offering__type=REMOTE_OFFERING,
            state=OrderStates.EXECUTING,
            type=OrderTypes.TERMINATE,
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
        qs = models.Offering.objects.filter(type=REMOTE_OFFERING)
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


class PullOfferingRobotAccounts(OfferingActionView):
    task = tasks.pull_offering_robot_accounts


class PushProjectData(OfferingActionView):
    task = tasks.RemoteProjectDataPushTask()


class SyncResourceProjectPermissions(GenericAPIView):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]

    serializer_class = EmptySerializer

    def post(self, request, uuid):
        qs = models.Resource.objects.filter(offering__type=REMOTE_OFFERING)
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

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RemoteSynchronisationSerializer},
        request=None,
    )
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
            raise ValidationError("UUID is invalid.")
        resource = models.Resource.objects.filter(uuid=resource_uuid).first()
        if resource is None:
            raise NotFound("A resource is not found")
        if resource.state == ResourceStates.TERMINATED:
            raise ValidationError("The resource is terminated")
        if resource.state == ResourceStates.UPDATING:
            raise ValidationError("The resource is updating")
        return resource

    serializer_class = EmptySerializer

    def post(self, *args, **kwargs):
        resource = self.get_resource()
        tasks.ResourcePullTask().apply_async(
            args=[serialize_instance(resource)], kwargs={}
        )
        return Response(status=status.HTTP_200_OK)


class PullResourceRobotAccountsView(GenericAPIView):
    permission_classes = [rf_permissions.IsAuthenticated, core_permissions.IsStaff]

    def get_resource(self):
        resource_uuid = self.kwargs["uuid"]
        if not is_uuid_like(resource_uuid):
            raise ValidationError("UUID is invalid.")
        resource = models.Resource.objects.filter(uuid=resource_uuid).first()
        if resource is None:
            raise NotFound("A resource is not found")
        if resource.state == ResourceStates.TERMINATED:
            raise ValidationError("The resource is terminated")
        if resource.state == ResourceStates.UPDATING:
            raise ValidationError("The resource is updating")
        if resource.offering.type != REMOTE_OFFERING:
            raise ValidationError("The resource offering is not remote")
        return resource

    serializer_class = EmptySerializer

    def post(self, *args, **kwargs):
        resource = self.get_resource()
        tasks.ResourceRobotAccountPullTask().apply_async(
            args=[serialize_instance(resource)], kwargs={}
        )
        return Response(status=status.HTTP_200_OK)


class RemoteResourceStatusView(GenericAPIView):
    """View for getting remote resource sync status"""

    permission_classes = [rf_permissions.IsAuthenticated]
    serializer_class = serializers.RemoteResourceSyncStatusSerializer

    @extend_schema(
        responses=serializers.RemoteResourceSyncStatusSerializer,
        description="Get remote resource sync status",
    )
    def get(self, request, **kwargs):
        """Get sync status for a remote resource"""
        local_resource_uuid = kwargs["resource_uuid"]
        if not is_uuid_like(local_resource_uuid):
            raise exceptions.ParseError(detail="UUID is invalid.")
        resource = get_object_or_404(models.Resource, uuid=local_resource_uuid)
        check_remote_resource_access(request, resource)

        sync_data = get_resource_sync_status(resource)
        serializer = self.get_serializer(sync_data, many=False)
        return Response(serializer.data)


class RemoteResourceOrderViewSet(GenericAPIView):
    """ViewSet for remote resource order operations"""

    permission_classes = [rf_permissions.IsAuthenticated]
    serializer_class = serializers.RemoteResourceOrderSerializer

    @extend_schema(
        responses=serializers.RemoteResourceOrderSerializer,
        description="Get remote order details",
    )
    def get(self, request, **kwargs):
        """Get detailed information about a remote order"""
        local_resource_uuid = kwargs["resource_uuid"]
        if not is_uuid_like(local_resource_uuid):
            raise exceptions.ParseError(detail="UUID is invalid.")
        resource = get_object_or_404(models.Resource, uuid=local_resource_uuid)
        check_remote_resource_access(request, resource)

        sync_data = get_resource_order_sync_status(resource)
        serializer = self.get_serializer(sync_data, many=True)
        return Response(serializer.data)


class RemoteResourceTeamViewSet(GenericAPIView):
    """ViewSet for remote resource team operations"""

    permission_classes = [rf_permissions.IsAuthenticated]
    serializer_class = serializers.RemoteResourceTeamMemberSerializer
    filter_backends = []
    ordering_fields = ["full_name", "remote_role", "sync_status", "local_role"]

    @extend_schema(
        responses=serializers.RemoteResourceTeamMemberSerializer(many=True),
        description="Get remote resource team members",
    )
    def get(self, request, **kwargs):
        """Get team members for a remote resource"""
        local_resource_uuid = kwargs["resource_uuid"]
        if not is_uuid_like(local_resource_uuid):
            raise ValidationError("UUID is invalid.")
        resource = get_object_or_404(models.Resource, uuid=local_resource_uuid)
        check_remote_resource_access(request, resource)
        team_data = get_resource_team(resource)
        ordering = request.query_params.get("o", "full_name")
        if ordering.startswith("-"):
            reverse = True
            field = ordering[1:]
        else:
            reverse = False
            field = ordering

        if field in self.ordering_fields:
            team_data.sort(key=lambda x: x.get(field, ""), reverse=reverse)

        serializer = self.get_serializer(team_data, many=True)
        return Response(serializer.data)
