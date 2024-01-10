from django.conf import settings
from rest_framework import response, status

from waldur_core.core import views as core_views
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace_openstack import executors, serializers


class MarketplaceTenantViewSet(core_views.ActionsViewSet):
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        skip = (
            settings.WALDUR_CORE["ONLY_STAFF_MANAGES_SERVICES"]
            and serializer.validated_data["skip_connection_extnet"]
        )
        executors.MarketplaceTenantCreateExecutor.execute(
            tenant, skip_connection_extnet=skip
        )

        return response.Response(
            {"uuid": tenant.uuid.hex}, status=status.HTTP_201_CREATED
        )

    serializer_class = serializers.MarketplaceTenantCreateSerializer
    create_permissions = [structure_permissions.check_access_to_services_management]
