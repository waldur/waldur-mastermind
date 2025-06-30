from django_filters.rest_framework import DjangoFilterBackend

from waldur_autoprovisioning import models
from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions

from . import serializers


class RuleViewSet(ActionsViewSet):
    queryset = models.Rule.objects.all().order_by("-customer")
    serializer_class = serializers.RuleSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]


class RulePlansViewSet(ActionsViewSet):
    queryset = models.RulePlans.objects.all().order_by("-rule")
    serializer_class = serializers.RulePlansSerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]
