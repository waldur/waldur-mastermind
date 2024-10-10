from rest_framework import response, status

from waldur_core.core import views as core_views
from waldur_mastermind.marketplace_openstack import serializers
from waldur_openstack.executors import TenantCreateExecutor


class MarketplaceTenantViewSet(core_views.ActionsViewSet):
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = serializer.save()
        skip = serializer.validated_data["skip_connection_extnet"]
        TenantCreateExecutor.execute(tenant, skip_connection_extnet=skip)

        return response.Response(
            {"uuid": tenant.uuid.hex}, status=status.HTTP_201_CREATED
        )

    serializer_class = serializers.MarketplaceTenantCreateSerializer
