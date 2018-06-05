from __future__ import unicode_literals

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import exceptions

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from . import serializers, models, filters


class ServiceProviderViewSet(core_views.ActionsViewSet):
    queryset = models.ServiceProvider.objects.all()
    serializer_class = serializers.ServiceProviderSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ServiceProviderFilter

    def staff_or_owner(request, view, obj=None):
        if not obj:
            return
        if request.user.is_staff:
            return
        if not structure_permissions._has_owner_access(request.user, obj.customer):
            raise exceptions.PermissionDenied()

    destroy_permissions = [staff_or_owner]
