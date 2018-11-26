from __future__ import unicode_literals

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from rest_framework import status, exceptions as rf_exceptions, viewsets as rf_viewsets
from rest_framework import views
from rest_framework.decorators import detail_route, list_route
from rest_framework.response import Response

from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views

from . import serializers, models, filters, tasks, plugins


class BaseMarketplaceView(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)
    update_permissions = \
        partial_update_permissions = \
        destroy_permissions = \
        [structure_permissions.is_owner]


class ServiceProviderViewSet(BaseMarketplaceView):
    queryset = models.ServiceProvider.objects.all()
    serializer_class = serializers.ServiceProviderSerializer
    filter_class = filters.ServiceProviderFilter
    api_secret_code_permissions = [structure_permissions.is_owner]

    @detail_route(methods=['GET', 'POST'])
    def api_secret_code(self, request, uuid=None):
        """ On GET request - return service provider api_secret_code.
            On POST - generate new service provider api_secret_code.
        """
        service_provider = self.get_object()
        if request.method == 'GET':
            return Response({
                'api_secret_code': service_provider.api_secret_code
            }, status=status.HTTP_200_OK)
        else:
            service_provider.generate_api_secret_code()
            service_provider.save()
            return Response({
                'detail': _('Api secret code updated.'),
                'api_secret_code': service_provider.api_secret_code
            }, status=status.HTTP_200_OK)

    def check_related_resources(request, view, obj=None):
        if obj and obj.has_active_offerings:
            raise rf_exceptions.ValidationError(_(
                'Service provider has active offerings. Please archive them first.'
            ))

    destroy_permissions = [structure_permissions.is_owner, check_related_resources]


class CategoryViewSet(EagerLoadMixin, core_views.ActionsViewSet):
    queryset = models.Category.objects.all()
    serializer_class = serializers.CategorySerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)

    create_permissions = \
        update_permissions = \
        partial_update_permissions = \
        destroy_permissions = \
        [structure_permissions.is_staff]


class OfferingViewSet(BaseMarketplaceView):
    queryset = models.Offering.objects.all()
    serializer_class = serializers.OfferingSerializer
    filter_class = filters.OfferingFilter
    filter_backends = (DjangoFilterBackend, filters.OfferingCustomersFilterBackend)

    @detail_route(methods=['post'])
    def activate(self, request, uuid=None):
        return self._update_state('activate')

    @detail_route(methods=['post'])
    def pause(self, request, uuid=None):
        return self._update_state('pause')

    @detail_route(methods=['post'])
    def archive(self, request, uuid=None):
        return self._update_state('archive')

    def _update_state(self, action):
        offering = self.get_object()

        try:
            getattr(offering, action)()
        except TransitionNotAllowed:
            raise rf_exceptions.ValidationError(_('Offering state is invalid.'))

        offering.save(update_fields=['state'])
        return Response({
            'detail': _('Offering state updated.'),
            'state': offering.state
        }, status=status.HTTP_200_OK)

    activate_permissions = \
        pause_permissions = \
        archive_permissions = \
        [structure_permissions.is_owner]


class PlanViewSet(BaseMarketplaceView):
    queryset = models.Plan.objects.all()
    serializer_class = serializers.PlanSerializer
    filter_class = filters.PlanFilter


class ScreenshotViewSet(BaseMarketplaceView):
    queryset = models.Screenshot.objects.all()
    serializer_class = serializers.ScreenshotSerializer
    filter_class = filters.ScreenshotFilter


class OrderViewSet(BaseMarketplaceView):
    queryset = models.Order.objects.all()
    serializer_class = serializers.OrderSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.OrderFilter

    def check_permissions_for_state_change(request, view, order=None):
        if not order:
            return

        user = request.user

        if user.is_staff:
            return

        if settings.WALDUR_MARKETPLACE['OWNER_CAN_APPROVE_ORDER'] and \
                structure_permissions._has_owner_access(user, order.project.customer):
            return

        if settings.WALDUR_MARKETPLACE['MANAGER_CAN_APPROVE_ORDER'] and \
                structure_permissions._has_manager_access(user, order.project):
            return

        if settings.WALDUR_MARKETPLACE['ADMIN_CAN_APPROVE_ORDER'] and \
                structure_permissions._has_admin_access(user, order.project):
            return

        raise rf_exceptions.PermissionDenied()

    @detail_route(methods=['post'])
    def approve(self, request, uuid=None):
        order = self.get_object()
        order.approve()
        order.approved_by = request.user
        order.approved_at = timezone.now()
        order.save()

        serialized_order = core_utils.serialize_instance(order)
        serialized_user = core_utils.serialize_instance(request.user)
        tasks.process_order.delay(serialized_order, serialized_user)
        tasks.create_order_pdf.delay(order.pk)

        return Response({'detail': _('Order has been approved.')}, status=status.HTTP_200_OK)

    approve_validators = [core_validators.StateValidator(models.Order.States.REQUESTED_FOR_APPROVAL)]
    approve_permissions = [check_permissions_for_state_change]

    @detail_route()
    def pdf(self, request, uuid=None):
        order = self.get_object()
        if not order.has_file():
            raise Http404()

        file_response = HttpResponse(order.file, content_type='application/pdf')
        filename = order.get_filename()
        file_response['Content-Disposition'] = 'attachment; filename="{filename}"'.format(filename=filename)
        return file_response


class PluginViewSet(views.APIView):
    def get(self, request):
        offering_types = plugins.manager.get_offering_types()
        payload = []
        for offering_type in offering_types:
            components = [
                dict(
                    type=component.type,
                    name=component.name,
                    measured_unit=component.measured_unit,
                    billing_type=component.billing_type,
                )
                for component in plugins.manager.get_components(offering_type)
            ]
            payload.append(dict(
                offering_type=offering_type,
                components=components,
            ))
        return Response(payload, status=status.HTTP_200_OK)


class CustomerOfferingViewSet(views.APIView):
    serializer_class = serializers.CustomerOfferingSerializer

    def _get_customer(self, request, uuid):
        user = request.user
        if not user.is_staff:
            raise rf_exceptions.PermissionDenied()

        return get_object_or_404(structure_models.Customer, uuid=uuid)

    def get(self, request, uuid):
        customer = self._get_customer(request, uuid)
        serializer = self.serializer_class(customer, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, uuid):
        customer = self._get_customer(request, uuid)
        serializer = self.serializer_class(instance=customer, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_200_OK)


class OrderItemViewSet(BaseMarketplaceView):
    queryset = models.OrderItem.objects.all()
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.OrderItemSerializer
    filter_class = filters.OrderItemFilter

    def check_permissions_for_order_items_change(request, view, order_item=None):
        if not order_item:
            return
        if order_item.order.state != models.Order.States.REQUESTED_FOR_APPROVAL:
            raise rf_exceptions.PermissionDenied()

    destroy_permissions = [check_permissions_for_order_items_change]


class CartItemViewSet(core_views.ActionsViewSet):
    queryset = models.CartItem.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.CartItemSerializer

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    @list_route(methods=['post'])
    def submit(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = serializer.save()
        order_serializer = serializers.OrderSerializer(instance=order, context={'request': self.request})
        return Response(order_serializer.data, status=status.HTTP_201_CREATED)

    submit_serializer_class = serializers.CartSubmitSerializer


class ResourceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Resource.objects.exclude(state=models.Resource.States.TERMINATED)
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ResourceFilter
    lookup_field = 'uuid'
    serializer_class = serializers.ResourceSerializer


class MarketplaceAPIViewSet(rf_viewsets.ViewSet):
    def get_action_class(self):
        return getattr(self, self.action + '_serializer_class', None)

    permission_classes = ()
    serializer_class = serializers.ServiceProviderSignatureSerializer
    set_usage_serializer_class = serializers.PublicListComponentUsageSerializer

    def get_validated_data(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.data['data']
        dry_run = serializer.validated_data['dry_run']
        data_serializer_class = self.get_action_class()

        if data_serializer_class:
            data_serializer = data_serializer_class(data=data)
            data_serializer.is_valid(raise_exception=True)
            return data_serializer.validated_data, dry_run

        return serializer.validated_data, dry_run

    @list_route(methods=['post'])
    @csrf_exempt
    def check_signature(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_200_OK)

    @list_route(methods=['post'])
    @csrf_exempt
    def set_usage(self, request, *args, **kwargs):
        validated_data, dry_run = self.get_validated_data(request)

        if not dry_run:
            usages = []
            for usage in validated_data['usages']:
                usages.append(models.ComponentUsage(resource=usage['resource'],
                                                    component=usage['component'],
                                                    date=usage['date'],
                                                    usage=usage['amount']))

            models.ComponentUsage.objects.bulk_create(usages)

        return Response(status=status.HTTP_201_CREATED)


def inject_project_resources_counter(project):
    return {
        'marketplace_category_{}'.format(counter.category.uuid): counter.count
        for counter in models.ProjectResourceCount.objects.filter(project=project).only('count', 'category')
    }


structure_views.ProjectCountersView.register_dynamic_counter(inject_project_resources_counter)
