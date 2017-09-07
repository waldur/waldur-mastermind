from __future__ import unicode_literals

from django.conf import settings

from rest_framework import exceptions

from nodeconductor.core import views as core_views
from nodeconductor.structure import models as structure_models
from nodeconductor.structure import permissions as structure_permissions

from . import filters, models, serializers


class PriceEstimateViewSet(core_views.ActionsViewSet):
    disabled_actions = ['create', 'destroy']
    queryset = models.PriceEstimate.objects.all()
    serializer_class = serializers.PriceEstimateSerializer
    lookup_field = 'uuid'
    filter_backends = (
        filters.PriceEstimateScopeFilterBackend,
    )

    def get_queryset(self):
        return models.PriceEstimate.objects.filtered_for_user(self.request.user)

    def is_owner_or_staff(request, view, obj=None):
        if not obj:
            return False
        if request.user.is_staff:
            return True

        customer = structure_permissions._get_customer(obj.scope)
        is_owner = customer.has_user(request.user, structure_models.CustomerRole.OWNER)
        if not is_owner or not settings.NODECONDUCTOR['OWNER_CAN_MODIFY_COST_LIMIT']:
            raise exceptions.PermissionDenied()

    update_permissions = [is_owner_or_staff]
