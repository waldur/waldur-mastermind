from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from waldur_client import WaldurClient, WaldurClientException

from waldur_core.core.utils import is_uuid_like, serialize_instance
from waldur_core.core.views import ReviewViewSet
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.filters import GenericRoleFilter
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace import models, permissions, plugins
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.constants import OFFERING_FIELDS
from waldur_mastermind.marketplace_remote.models import ProjectUpdateRequest

from . import filters, serializers, tasks, utils


class RemoteView(APIView):
    def get_client(self, request):
        serializer = serializers.CredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        api_url = serializer.validated_data['api_url']
        token = serializer.validated_data['token']
        return WaldurClient(api_url, token)


class CustomersView(RemoteView):
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        params = {
            'owned_by_current_user': True,
            'field': ['uuid', 'name', 'abbreviation', 'phone_number', 'email'],
        }
        try:
            customers = client.list_customers(params)
        except WaldurClientException as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        return Response(customers)


class OfferingsListView(RemoteView):
    def post(self, request, *args, **kwargs):
        client = self.get_client(request)
        if 'customer_uuid' not in request.query_params:
            raise ValidationError(
                {'url': _('customer_uuid field must be present in query parameters')}
            )

        remote_customer_uuid = request.query_params['customer_uuid']
        whitelist_types = [
            offering_type
            for offering_type in plugins.manager.get_offering_types()
            if plugins.manager.enable_remote_support(offering_type)
        ]

        params = {
            'shared': True,
            'allowed_customer_uuid': remote_customer_uuid,
            'type': whitelist_types,
            'field': ['uuid', 'name', 'type', 'state', 'category_title'],
        }
        try:
            remote_offerings = client.list_marketplace_offerings(params)
        except WaldurClientException as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        local_offerings = list(
            models.Offering.objects.filter(type=PLUGIN_NAME)
            .exclude(state=models.Offering.States.ARCHIVED)
            .values_list('backend_id', flat=True)
        )

        importable_offerings = [
            offering
            for offering in remote_offerings
            if offering['uuid'] not in local_offerings
        ]
        return Response(importable_offerings)


class OfferingCreateView(RemoteView):
    def post(self, request, *args, **kwargs):
        serializer = serializers.OfferingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        client = self.get_client(request)

        api_url = serializer.validated_data['api_url']
        token = serializer.validated_data['token']
        remote_offering_uuid = serializer.validated_data['remote_offering_uuid']
        remote_customer_uuid = serializer.validated_data['remote_customer_uuid']
        local_customer_uuid = serializer.validated_data['local_customer_uuid']
        local_category_uuid = serializer.validated_data['local_category_uuid']

        local_customer = Customer.objects.get(uuid=local_customer_uuid)
        local_category = models.Category.objects.get(uuid=local_category_uuid)

        try:
            remote_offering = client.get_marketplace_offering(remote_offering_uuid)
        except WaldurClientException as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        secret_options = {
            'api_url': api_url,
            'token': token,
            'customer_uuid': remote_customer_uuid,
        }
        local_offering = self.import_offering(
            remote_offering, local_customer, local_category, secret_options
        )

        return Response({'uuid': local_offering.uuid.hex})

    def import_offering(
        self, remote_offering, local_customer, local_category, secret_options
    ):
        local_offering = models.Offering.objects.create(
            type=PLUGIN_NAME,
            billable=True,
            backend_id=remote_offering['uuid'],
            customer=local_customer,
            category=local_category,
            secret_options=secret_options,
            **{key: remote_offering[key] for key in OFFERING_FIELDS}
        )
        local_components_map = utils.import_offering_components(
            local_offering, remote_offering
        )
        utils.import_plans(local_offering, remote_offering, local_components_map)
        return local_offering


class ProjectUpdateRequestViewSet(ReviewViewSet):
    queryset = ProjectUpdateRequest.objects.all()
    approve_permissions = reject_permissions = [
        permissions.user_is_service_provider_owner_or_service_provider_manager
    ]
    serializer_class = serializers.ProjectUpdateRequestSerializer
    filter_backends = [GenericRoleFilter, DjangoFilterBackend]
    filterset_class = filters.ProjectUpdateRequestFilter


class PullOrderItemView(APIView):
    permission_classes = []

    def get_order_item(self):
        item_uuid = self.kwargs['uuid']
        if not is_uuid_like(item_uuid):
            return Response(status=status.HTTP_400_BAD_REQUEST, data='UUID is invalid.')
        qs = models.OrderItem.objects.filter(offering__type=PLUGIN_NAME).exclude(
            state__in=models.OrderItem.States.TERMINAL_STATES
        )
        return get_object_or_404(qs, uuid=item_uuid)

    def post(self, *args, **kwargs):
        order_item = self.get_order_item()
        tasks.OrderItemPullTask.apply_async(args=[serialize_instance(order_item)])
        return Response(status=status.HTTP_200_OK)


class OfferingActionView(APIView):
    def post(self, request, uuid):
        qs = models.Offering.objects.filter(type=PLUGIN_NAME)
        offering = get_object_or_404(qs, uuid=uuid)
        if not structure_permissions._has_owner_access(
            request.user, offering.customer
        ) and not offering.customer.has_user(
            request.user, role=structure_models.CustomerRole.SERVICE_MANAGER
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


class PullOfferingOrderItems(OfferingActionView):
    task = tasks.pull_offering_order_items


class PullOfferingUsage(OfferingActionView):
    task = tasks.pull_offering_usage


class PullOfferingInvoices(OfferingActionView):
    task = tasks.pull_offering_invoices
