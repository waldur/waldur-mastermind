from __future__ import unicode_literals

from django.db import transaction
from django.db.models import Count, OuterRef, Subquery, F, ExpressionWrapper, PositiveSmallIntegerField
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from django_fsm import TransitionNotAllowed
from rest_framework import status, exceptions as rf_exceptions, viewsets as rf_viewsets
from rest_framework import views
from rest_framework.decorators import detail_route, list_route
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.core.utils import order_with_nulls
from waldur_core.structure import models as structure_models
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views
from waldur_core.structure.permissions import _has_owner_access
from waldur_core.structure import utils as structure_utils

from . import serializers, models, filters, tasks, plugins, permissions


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


def can_update_offering(request, view, obj=None):
    offering = obj

    if not offering:
        return

    if offering.state == models.Offering.States.DRAFT:
        structure_permissions.is_owner(request, view, offering)
    else:
        structure_permissions.is_staff(request, view)


def validate_offering_update(offering):
    if offering.state == models.Offering.States.ARCHIVED:
        raise rf_exceptions.ValidationError(_('It is not possible to update archived offering.'))


class OfferingViewSet(BaseMarketplaceView):
    queryset = models.Offering.objects.all()
    serializer_class = serializers.OfferingDetailsSerializer
    create_serializer_class = serializers.OfferingCreateSerializer
    update_serializer_class = partial_update_serializer_class = serializers.OfferingUpdateSerializer
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

    activate_validators = \
        pause_validators = \
        archive_validators = \
        destroy_validators = \
        [structure_utils.check_customer_blocked]

    update_permissions = \
        partial_update_permissions = \
        [can_update_offering]

    update_validators = \
        partial_update_validators = \
        [validate_offering_update, structure_utils.check_customer_blocked]

    def perform_create(self, serializer):
        customer = serializer.validated_data['customer']
        structure_utils.check_customer_blocked(customer)

        super(OfferingViewSet, self).perform_create(serializer)


class PlanUsageReporter(object):
    """
    This class provides aggregate counts of how many plans of a
    certain type for each offering is used.
    """
    def __init__(self, view, request):
        self.view = view
        self.request = request

    def get_report(self):
        plans = models.Plan.objects.all()

        query = self.parse_query()
        if query:
            plans = self.apply_filters(query, plans)

        resources = self.get_subquery()
        remaining = ExpressionWrapper(
            F('limit') - F('usage'),
            output_field=PositiveSmallIntegerField()
        )
        plans = plans.annotate(usage=Subquery(resources[:1]), limit=F('max_amount'))\
            .annotate(remaining=remaining)
        plans = self.apply_ordering(plans)

        return self.serialize(plans)

    def parse_query(self):
        if self.request.query_params:
            serializer = serializers.PlanUsageRequestSerializer(data=self.request.query_params)
            serializer.is_valid(raise_exception=True)
            return serializer.validated_data
        return None

    def get_subquery(self):
        # Aggregate
        resources = models.Resource.objects \
            .filter(plan_id=OuterRef('pk')) \
            .exclude(state=models.Resource.States.TERMINATED) \
            .annotate(count=Count('*')) \
            .values_list('count', flat=True)

        # Workaround for Django bug:
        # https://code.djangoproject.com/ticket/28296
        # It allows to remove extra GROUP BY clause from the subquery.
        resources.query.group_by = []

        return resources

    def apply_filters(self, query, plans):
        if query.get('offering_uuid'):
            plans = plans.filter(offering__uuid=query.get('offering_uuid'))

        if query.get('customer_provider_uuid'):
            plans = plans.filter(offering__customer__uuid=query.get('customer_provider_uuid'))

        return plans

    def apply_ordering(self, plans):
        param = self.request.query_params and self.request.query_params.get('o') or '-usage'
        return order_with_nulls(plans, param)

    def serialize(self, plans):
        page = self.view.paginate_queryset(plans)
        serializer = serializers.PlanUsageResponseSerializer(page, many=True)
        return self.view.get_paginated_response(serializer.data)


def validate_plan_update(plan):
    if models.Resource.objects.filter(plan=plan).exists():
        raise rf_exceptions.ValidationError(_('It is not possible to update plan because it is used by resources.'))


def validate_plan_archive(plan):
    if plan.archived:
        raise rf_exceptions.ValidationError(_('Plan is already archived.'))


class PlanViewSet(BaseMarketplaceView):
    queryset = models.Plan.objects.all()
    serializer_class = serializers.PlanDetailsSerializer
    filter_class = filters.PlanFilter

    disabled_actions = ['destroy']
    update_validators = partial_update_validators = [validate_plan_update]

    archive_permissions = [structure_permissions.is_owner]
    archive_validators = [validate_plan_archive]

    @detail_route(methods=['post'])
    def archive(self, request, uuid=None):
        plan = self.get_object()
        plan.archived = True
        plan.save(update_fields=['archived'])
        return Response({'detail': _('Plan has been archived.')}, status=status.HTTP_200_OK)

    @list_route()
    def usage_stats(self, request):
        return PlanUsageReporter(self, request).get_report()


class ScreenshotViewSet(BaseMarketplaceView):
    queryset = models.Screenshot.objects.all()
    serializer_class = serializers.ScreenshotSerializer
    filter_class = filters.ScreenshotFilter


class OrderViewSet(BaseMarketplaceView):
    queryset = models.Order.objects.all()
    serializer_class = serializers.OrderSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.OrderFilter
    destroy_validators = partial_update_validators = [structure_utils.check_customer_blocked]

    @detail_route(methods=['post'])
    def approve(self, request, uuid=None):
        tasks.approve_order(self.get_object(), request.user)

        return Response({'detail': _('Order has been approved.')}, status=status.HTTP_200_OK)

    approve_validators = [core_validators.StateValidator(models.Order.States.REQUESTED_FOR_APPROVAL),
                          structure_utils.check_customer_blocked]
    approve_permissions = [permissions.check_permissions_for_state_change]

    @detail_route(methods=['post'])
    def reject(self, request, uuid=None):
        order = self.get_object()
        order.reject()
        order.save(update_fields=['state'])
        return Response({'detail': _('Order has been rejected.')}, status=status.HTTP_200_OK)

    def check_permissions_for_reject(request, view, order=None):
        if not order:
            return

        user = request.user

        if user.is_staff:
            return

        if user == order.created_by:
            return

        raise rf_exceptions.PermissionDenied()

    reject_validators = [core_validators.StateValidator(models.Order.States.REQUESTED_FOR_APPROVAL),
                         structure_utils.check_customer_blocked]
    reject_permissions = [check_permissions_for_reject]

    @detail_route()
    def pdf(self, request, uuid=None):
        order = self.get_object()
        if not order.has_file():
            raise Http404()

        file_response = HttpResponse(order.file, content_type='application/pdf')
        filename = order.get_filename()
        file_response['Content-Disposition'] = 'attachment; filename="{filename}"'.format(filename=filename)
        return file_response

    def perform_create(self, serializer):
        project = serializer.validated_data['project']
        structure_utils.check_customer_blocked(project)

        super(OrderViewSet, self).perform_create(serializer)


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
    serializer_class = serializers.OrderItemDetailsSerializer
    filter_class = filters.OrderItemFilter

    def order_items_destroy_validator(order_item):
        if not order_item:
            return
        if order_item.order.state != models.Order.States.REQUESTED_FOR_APPROVAL:
            raise rf_exceptions.PermissionDenied()

    destroy_validators = [order_items_destroy_validator]
    destroy_permissions = terminate_permissions = [structure_permissions.is_administrator]

    @detail_route(methods=['post'])
    def terminate(self, request, uuid=None):
        order_item = self.get_object()
        if not plugins.manager.can_terminate_order_item(order_item.offering.type):
            return Response({
                'details': 'Order item could not be terminated because it is not supported by plugin.'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            # It is expected that plugin schedules Celery task to call backend
            # and then switches order item to terminated state.
            order_item.set_state_terminating()
            order_item.save(update_fields=['state'])
        except TransitionNotAllowed:
            return Response({
                'details': 'Order item could not be terminated because it has been already processed.'
            }, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'details': 'Order item termination has been scheduled.'
        }, status=status.HTTP_202_ACCEPTED)


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
        order_serializer = serializers.OrderSerializer(instance=order, context=self.get_serializer_context())
        return Response(order_serializer.data, status=status.HTTP_201_CREATED)

    submit_serializer_class = serializers.CartSubmitSerializer


class ResourceViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.Resource.objects.exclude(state=models.Resource.States.TERMINATED)
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.ResourceScopeFilterBackend
    )
    filter_class = filters.ResourceFilter
    lookup_field = 'uuid'
    serializer_class = serializers.ResourceSerializer

    @detail_route(methods=['post'])
    def terminate(self, request, uuid=None):
        resource = self.get_object()

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
                type=models.OrderItem.Types.TERMINATE,
            )
            order = serializers.create_order(
                project=resource.project,
                user=self.request.user,
                items=[order_item],
                request=request,
            )

        return Response({'order_uuid': order.uuid}, status=status.HTTP_200_OK)

    @detail_route(methods=['post'])
    def switch_plan(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.validated_data['plan']

        with transaction.atomic():
            order_item = models.OrderItem(
                resource=resource,
                offering=resource.offering,
                old_plan=resource.plan,
                plan=plan,
                type=models.OrderItem.Types.UPDATE,
                limits=resource.limits or {},
            )
            order = serializers.create_order(
                project=resource.project,
                user=self.request.user,
                items=[order_item],
                request=request,
            )

        return Response({'order_uuid': order.uuid}, status=status.HTTP_200_OK)

    switch_plan_serializer_class = serializers.ResourceSwitchPlanSerializer

    terminate_permissions = \
        switch_plan_permissions = [structure_permissions.is_administrator]
    switch_plan_validators = \
        terminate_validators = [core_validators.StateValidator(models.Resource.States.OK),
                                structure_utils.check_customer_blocked]

    @detail_route(methods=['get'])
    def plan_periods(self, request, uuid=None):
        resource = self.get_object()
        qs = models.ResourcePlanPeriod.objects.filter(resource=resource)
        serializer = serializers.ResourcePlanPeriodSerializer(qs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class CategoryComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.CategoryComponentUsage.objects.all().order_by('-date', 'component__type')
    filter_backends = (DjangoFilterBackend, filters.CategoryComponentUsageScopeFilterBackend)
    filter_class = filters.CategoryComponentUsageFilter
    serializer_class = serializers.CategoryComponentUsageSerializer


class ComponentUsageViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.ComponentUsage.objects.all().order_by('-date', 'component__type')
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ComponentUsageFilter
    serializer_class = serializers.ComponentUsageSerializer

    @list_route(methods=['post'])
    def set_usage(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource = serializer.validated_data['plan_period'].resource
        if not _has_owner_access(request.user, resource.offering.customer):
            raise PermissionDenied(
                _('Only staff and service provider owner is allowed '
                  'to submit usage data for marketplace resource.')
            )
        serializer.save()
        return Response(status=status.HTTP_201_CREATED)

    set_usage_serializer_class = serializers.ComponentUsageCreateSerializer


class MarketplaceAPIViewSet(rf_viewsets.ViewSet):
    """
    TODO: Move this viewset to  ComponentUsageViewSet.
    """

    permission_classes = ()
    serializer_class = serializers.ServiceProviderSignatureSerializer

    def get_validated_data(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data['data']
        dry_run = serializer.validated_data['dry_run']

        if self.action == 'set_usage':
            data_serializer = serializers.ComponentUsageCreateSerializer(data=data)
            data_serializer.is_valid(raise_exception=True)
            if not dry_run:
                data_serializer.save()

        return serializer.validated_data, dry_run

    @list_route(methods=['post'])
    @csrf_exempt
    def check_signature(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_200_OK)

    @list_route(methods=['post'])
    @csrf_exempt
    def set_usage(self, request, *args, **kwargs):
        self.get_validated_data(request)
        return Response(status=status.HTTP_201_CREATED)


class OfferingFileViewSet(core_views.ActionsViewSet):
    queryset = models.OfferingFile.objects.all()
    filter_class = filters.OfferingFileFilter
    filter_backends = [DjangoFilterBackend]
    serializer_class = serializers.OfferingFileSerializer
    lookup_field = 'uuid'
    disabled_actions = ['update', 'partial_update']

    def check_create_permissions(request, view, obj=None):
        serializer_class = view.get_serializer_class()
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        offering = serializer.validated_data['offering']

        if user.is_staff or \
                (offering.customer and offering.customer.has_user(user, structure_models.CustomerRole.OWNER)):
            return

        raise rf_exceptions.PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [structure_permissions.is_owner]


for view in (structure_views.ProjectCountersView, structure_views.CustomerCountersView):
    def inject_resources_counter(scope):
        counters = models.AggregateResourceCount.objects.filter(scope=scope).only('count', 'category')
        return {
            'marketplace_category_{}'.format(counter.category.uuid): counter.count
            for counter in counters
        }

    view.register_dynamic_counter(inject_resources_counter)
