import logging

from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import PermissionDenied

from waldur_core.core import views as core_views
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)

from . import executors, filters, models, serializers

logger = logging.getLogger(__name__)


class LexisLinkViewSet(core_views.ActionsViewSet):
    queryset = models.LexisLink.objects.all()
    lookup_field = "uuid"
    serializer_class = serializers.LexisLinkSerializer
    create_serializer_class = serializers.LexisLinkCreateSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.LexisLinkFilter

    def check_create_permissions(request, view, obj=None):
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource = serializer.validated_data.get("resource")
        if not resource:
            raise PermissionDenied()

        # Check global permission first (like the "*" in original permission factory)
        if has_permission(request, PermissionEnum.CREATE_LEXIS_LINK, None):
            return

        # Check permission on resource's offering customer (service provider)
        if has_permission(
            request, PermissionEnum.CREATE_LEXIS_LINK, resource.offering.customer
        ):
            return

        raise PermissionDenied()

    create_permissions = [check_create_permissions]
    destroy_permissions = [
        permission_factory(
            PermissionEnum.DELETE_LEXIS_LINK,
            ["*", "robot_account.resource.offering.customer"],
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
        # Set robot account to requested deletion state
        try:
            robot_account = instance.robot_account
            robot_account.request_deletion()
            robot_account.save()
        except Exception as e:
            logger.error(
                "Failed to set robot account %s to deletion state: %s",
                robot_account,
                str(e),
            )

        executors.SshKeyDeleteExecutor().execute(instance)
