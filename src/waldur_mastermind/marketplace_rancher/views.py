from typing import cast

from celery import chain
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from waldur_core.core.models import User
from waldur_core.core.utils import serialize_instance
from waldur_core.core.views import ReadOnlyActionsViewSet
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import RANCHER_OFFERING
from waldur_mastermind.marketplace.serializers import ResourceSerializer
from waldur_mastermind.marketplace_rancher import tasks
from waldur_mastermind.marketplace_rancher.serializers import (
    ManagedRancherCreateNodeSerializer,
)
from waldur_openstack.models import Tenant
from waldur_rancher import executors as rancher_executors
from waldur_rancher import serializers as rancher_serializers
from waldur_rancher import utils as rancher_utils
from waldur_rancher.models import Cluster


class ManagedRancherViewSet(ReadOnlyActionsViewSet):
    queryset = marketplace_models.Resource.objects.filter(
        offering__type=RANCHER_OFFERING,
    )
    lookup_field = "uuid"
    serializer_class = ResourceSerializer

    @extend_schema(
        request=ManagedRancherCreateNodeSerializer,
        responses=rancher_serializers.RancherNodeSerializer,
    )
    @action(detail=True, methods=["post"])
    def add_node(self, request, *args, **kwargs):
        cluster = cast(Cluster, self.get_object().scope)
        serializer = self.get_serializer(
            data=request.data, context={"cluster": cluster}
        )
        serializer.is_valid(raise_exception=True)
        node = serializer.save()

        selected_tenant = Tenant.objects.get(uuid=node.initial_data["tenant"])
        try:
            tenant_resource = marketplace_models.Resource.objects.get(
                scope=selected_tenant
            )
        except marketplace_models.Resource.DoesNotExist:
            raise ValidationError(
                f"Marketplace resource for the tenant {selected_tenant} is not found."
            )

        new_limits = {}
        for quota_name, limit_name in {
            "storage": "storage",
            "vcpu": "cores",
            "ram": "ram",
        }.items():
            limit = selected_tenant.get_quota_limit(quota_name)
            usage = selected_tenant.get_quota_usage(quota_name)
            free_quota = limit - usage
            requested_quota = rancher_utils.get_node_quota(quota_name, node.__dict__)
            if free_quota < requested_quota:
                new_limits[limit_name] = limit + (requested_quota - free_quota)
            else:
                new_limits[limit_name] = limit

        serialized_tenant_resource = serialize_instance(tenant_resource)
        _tasks = []

        if new_limits != tenant_resource.limits:
            _tasks.append(
                tasks.update_tenant_limits.si(serialized_tenant_resource, new_limits)
            )

        _tasks.append(
            rancher_executors.NodeCreateExecutor.as_signature(
                node,
                user_id=cast(User, self.request.user).id,
            )
        )
        transaction.on_commit(lambda: chain(*_tasks).apply_async())
        response_data = rancher_serializers.RancherNodeSerializer(
            instance=node, context={"request": request}
        ).data
        return Response(data=response_data, status=status.HTTP_202_ACCEPTED)

    add_node_serializer_class = ManagedRancherCreateNodeSerializer
    add_node_permissions = [
        permission_factory(
            PermissionEnum.APPROVE_ORDER,
            ["project.customer"],
        )
    ]
