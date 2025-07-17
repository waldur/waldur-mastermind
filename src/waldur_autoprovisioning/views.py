from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema

from waldur_autoprovisioning import models
from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions

from . import serializers


@extend_schema(
    tags=["Autoprovisioning Rules"], description="Manage autoprovisioning rules."
)
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
