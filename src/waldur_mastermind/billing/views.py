from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions

from nodeconductor.core import views as core_views
from nodeconductor.structure import models as structure_models

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

        if isinstance(obj.scope, structure_models.Customer):
            raise exceptions.PermissionDenied(
                _('Only staff is allowed to modify policy for the customer.')
            )

        elif isinstance(obj.scope, structure_models.Project):
            customer = obj.scope.customer
            if not customer.has_user(request.user, structure_models.CustomerRole.OWNER):
                raise exceptions.PermissionDenied(
                    _('Only staff and customer owner is allowed to modify policy for the project.')
                )

    update_permissions = [is_owner_or_staff]
