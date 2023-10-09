from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.core import views as core_views
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)

from . import executors, filters, models, serializers


class LexisLinkViewSet(core_views.ActionsViewSet):
    queryset = models.LexisLink.objects.all()
    lookup_field = 'uuid'
    serializer_class = serializers.LexisLinkSerializer
    create_serializer_class = serializers.LexisLinkCreateSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.LexisLinkFilter

    create_permissions = [
        permission_factory(
            PermissionEnum.CREATE_LEXIS_LINK_PERMISSION,
            ['*', 'robot_account.resource.offering.customer'],
        )
    ]
    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_LEXIS_LINK_PERMISSION,
            ['*', 'robot_account.resource.offering.customer'],
        )
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_staff or user.is_support:
            return qs
        customers = get_connected_customers(user)
        projects = get_connected_projects(user)
        subquery = (
            Q(robot_account__resource__project__in=projects)
            | Q(robot_account__resource__project__customer__in=customers)
            | Q(robot_account__resource__offering__customer__in=customers)
        )
        return qs.filter(subquery)

    def perform_destroy(self, instance):
        executors.SshKeyDeleteExecutor().execute(instance)
