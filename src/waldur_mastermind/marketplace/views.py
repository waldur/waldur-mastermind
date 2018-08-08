from __future__ import unicode_literals

from django.conf import settings
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, exceptions as rf_exceptions
from rest_framework.decorators import detail_route
from rest_framework.response import Response

from waldur_core.core import views as core_views, validators as core_validators
from waldur_core.core.mixins import EagerLoadMixin
from waldur_core.structure import permissions as structure_permissions, filters as structure_filters

from . import serializers, models, filters


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


class ScreenshotViewSet(BaseMarketplaceView):
    queryset = models.Screenshots.objects.all()
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
    def set_state_requested_for_approval(self, request, uuid=None):
        return self._update_state(request, models.Order.States.REQUESTED_FOR_APPROVAL)

    set_state_requested_for_approval_validators = [core_validators.StateValidator(models.Order.States.DRAFT)]

    @detail_route(methods=['post'])
    def set_state_executing(self, request, uuid=None):
        order = self.get_object()
        for item in order.items.all():
            item.process(request)
        return self._update_state(request, models.Order.States.EXECUTING)

    set_state_executing_validators = [core_validators.StateValidator(models.Order.States.REQUESTED_FOR_APPROVAL)]
    set_state_executing_permissions = [check_permissions_for_state_change]

    @detail_route(methods=['post'])
    def set_state_done(self, request, uuid=None):
        order = self.get_object()
        response = self._update_state(request, models.Order.States.DONE, order)
        order.approved_by = request.user
        order.approved_at = timezone.now()
        order.save()
        return response

    set_state_done_validators = [core_validators.StateValidator(models.Order.States.EXECUTING)]
    set_state_done_permissions = [check_permissions_for_state_change]

    @detail_route(methods=['post'])
    def set_state_terminated(self, request, uuid=None):
        return self._update_state(request, models.Order.States.TERMINATED)

    def _update_state(self, request, state, order=None):
        if not order:
            order = self.get_object()

        state_name = filter(lambda x: x[0] == state, models.Order.States.CHOICES)[0][1]
        state_name = state_name.replace(' ', '_')
        getattr(order, 'set_state_' + state_name)()
        order.save(update_fields=['state'])
        return Response({'detail': _('Order state updated.')},
                        status=status.HTTP_200_OK)
