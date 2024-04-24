import logging

from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, exceptions, response, status
from rest_framework import serializers as rf_serializers

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.logging.loggers import event_logger
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views
from waldur_openstack.openstack_base import views as openstack_base_views

from . import executors, filters, models, serializers

logger = logging.getLogger(__name__)


class FlavorViewSet(openstack_base_views.FlavorViewSet):
    queryset = models.Flavor.objects.all().order_by("settings", "cores", "ram", "disk")
    serializer_class = serializers.FlavorSerializer
    lookup_field = "uuid"
    filterset_class = filters.FlavorFilter


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all().order_by("name")
    serializer_class = serializers.ImageSerializer
    lookup_field = "uuid"
    filterset_class = filters.ImageFilter


class VolumeTypeViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.VolumeType.objects.all().order_by("settings", "name")
    serializer_class = serializers.VolumeTypeSerializer
    lookup_field = "uuid"
    filterset_class = filters.VolumeTypeFilter


class SecurityGroupViewSet(structure_views.ResourceViewSet):
    queryset = models.SecurityGroup.objects.all().order_by("tenant__name")
    serializer_class = serializers.SecurityGroupSerializer
    filterset_class = filters.SecurityGroupFilter
    disabled_actions = ["create"]
    pull_executor = executors.SecurityGroupPullExecutor

    def default_security_group_validator(security_group):
        if security_group.name == "default":
            raise exceptions.ValidationError(
                {"name": _("Default security group is managed by OpenStack itself.")}
            )

    update_validators = partial_update_validators = (
        structure_views.ResourceViewSet.update_validators
        + [default_security_group_validator]
    )
    update_executor = executors.SecurityGroupUpdateExecutor
    partial_update_serializer_class = update_serializer_class = (
        serializers.SecurityGroupUpdateSerializer
    )

    destroy_validators = structure_views.ResourceViewSet.destroy_validators + [
        default_security_group_validator
    ]
    delete_executor = executors.SecurityGroupDeleteExecutor

    @decorators.action(detail=True, methods=["POST"])
    def set_rules(self, request, uuid=None):
        """WARNING! Auto-generated HTML form is wrong for this endpoint. List should be defined as input.

        Example:
        [
            {
                "protocol": "tcp",
                "from_port": 1,
                "to_port": 10,
                "cidr": "10.1.1.0/24"
            }
        ]
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        security_group = self.get_object()
        old_rules = serializers.DebugSecurityGroupRuleSerializer(
            security_group.rules.all(), many=True
        )

        logger.info(
            "About to set rules for security group with ID %s. Old rules: %s. New rules: %s",
            security_group.id,
            old_rules.data,
            request.data,
        )

        serializer.save()
        security_group.refresh_from_db()

        executors.PushSecurityGroupRulesExecutor().execute(security_group)
        return response.Response(
            {"status": _("Rules update was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    set_rules_validators = [core_validators.StateValidator(models.Tenant.States.OK)]
    set_rules_serializer_class = serializers.SecurityGroupRuleListUpdateSerializer


class ServerGroupViewSet(structure_views.ResourceViewSet):
    queryset = models.ServerGroup.objects.all().order_by("tenant__name")
    serializer_class = serializers.ServerGroupSerializer
    filterset_class = filters.ServerGroupFilter
    pull_executor = executors.ServerGroupPullExecutor
    delete_executor = executors.ServerGroupDeleteExecutor


class FloatingIPViewSet(structure_views.ResourceViewSet):
    queryset = models.FloatingIP.objects.all().order_by("address")
    serializer_class = serializers.FloatingIPSerializer
    filterset_class = filters.FloatingIPFilter
    disabled_actions = ["update", "partial_update", "create"]
    delete_executor = executors.FloatingIPDeleteExecutor
    pull_executor = executors.FloatingIPPullExecutor

    def list(self, request, *args, **kwargs):
        """
        To get a list of all available floating IPs, issue **GET** against */api/floating-ips/*.
        Floating IPs are read only. Each floating IP has fields: 'address', 'status'.

        Status *DOWN* means that floating IP is not linked to a VM, status *ACTIVE* means that it is in use.
        """

        return super().list(request, *args, **kwargs)

    @decorators.action(detail=True, methods=["post"])
    def attach_to_port(self, request, uuid=None):
        floating_ip: models.FloatingIP = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        port: models.Port = serializer.validated_data["port"]
        if port.state != models.Port.States.OK:
            raise core_exceptions.IncorrectStateException(
                _(
                    "The port [%(port)s] is expected to have [OK] state, but actual one is [%(state)s]"
                )
                % {"port": port, "state": port.get_state_display()}
            )
        if port.tenant != floating_ip.tenant:
            raise exceptions.ValidationError(
                {
                    "detail": _(
                        "The port [%(port)s] is expected to belong to the same tenant [%(tenant)s] , but actual one is [%(actual_tenant)s]"
                    )
                    % {
                        "port": port,
                        "tenant": floating_ip.tenant,
                        "actual_tenant": port.tenant,
                    }
                }
            )

        executors.FloatingIPAttachExecutor().execute(
            floating_ip, port=core_utils.serialize_instance(port)
        )
        return response.Response(
            {"status": _("attaching was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    attach_to_port_serializer_class = serializers.FloatingIPAttachSerializer
    attach_to_port_validators = [
        core_validators.StateValidator(models.FloatingIP.States.OK)
    ]

    @decorators.action(detail=True, methods=["post"])
    def detach_from_port(self, request=None, uuid=None):
        floating_ip: models.FloatingIP = self.get_object()
        if not floating_ip.port:
            raise exceptions.ValidationError(
                {
                    "port": _("Floating IP [%(fip)s] is not attached to any port.")
                    % {"fip": floating_ip}
                }
            )
        executors.FloatingIPDetachExecutor().execute(floating_ip)
        return response.Response(
            {"status": _("detaching was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    detach_from_port_validators = [
        core_validators.StateValidator(models.FloatingIP.States.OK)
    ]

    @decorators.action(detail=True, methods=["post"])
    def update_description(self, request=None, uuid=None):
        floating_ip: models.FloatingIP = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        description = serializer.validated_data["description"]
        floating_ip.description = description
        floating_ip.save()
        executors.FloatingIPUpdateExecutor.execute(
            floating_ip, description=description, updated_fields=["description"]
        )
        return response.Response(
            {"status": _("Description was updated")}, status=status.HTTP_202_ACCEPTED
        )

    update_description_serializer_class = (
        serializers.FloatingIPDescriptionUpdateSerializer
    )
    update_description_validators = [
        core_validators.StateValidator(models.FloatingIP.States.OK)
    ]


class TenantViewSet(structure_views.ResourceViewSet):
    queryset = models.Tenant.objects.all().order_by("name")
    serializer_class = serializers.TenantSerializer
    filterset_class = structure_filters.BaseResourceFilter

    create_executor = executors.TenantCreateExecutor
    update_executor = executors.TenantUpdateExecutor
    pull_executor = executors.TenantPullExecutor

    def delete_permission_check(request, view, obj=None):
        if not obj:
            return
        if obj.service_settings.shared:
            if has_permission(
                request, PermissionEnum.APPROVE_ORDER, obj.project
            ) or has_permission(
                request, PermissionEnum.APPROVE_ORDER, obj.project.customer
            ):
                return
            raise exceptions.PermissionDenied()
        else:
            structure_permissions.is_administrator(
                request,
                view,
                obj,
            )

    delete_executor = executors.TenantDeleteExecutor
    destroy_permissions = [
        delete_permission_check,
        structure_permissions.check_access_to_services_management,
    ]
    create_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.check_access_to_services_management,
    ]

    @decorators.action(detail=True, methods=["post"])
    def set_quotas(self, request, uuid=None):
        """
        A quota can be set for a particular tenant. Only staff users can do that.
        In order to set quota submit **POST** request to */api/openstack-tenants/<uuid>/set_quotas/*.
        The quota values are propagated to the backend.

        The following quotas are supported. All values are expected to be integers:

        - instances - maximal number of created instances.
        - ram - maximal size of ram for allocation. In MiB_.
        - storage - maximal size of storage for allocation. In MiB_.
        - vcpu - maximal number of virtual cores for allocation.
        - security_group_count - maximal number of created security groups.
        - security_group_rule_count - maximal number of created security groups rules.
        - volumes - maximal number of created volumes.
        - snapshots - maximal number of created snapshots.

        It is possible to update quotas by one or by submitting all the fields in one request.
        Waldur will attempt to update the provided quotas. Please note, that if provided quotas are
        conflicting with the backend (e.g. requested number of instances is below of the already existing ones),
        some quotas might not be applied.

        .. _MiB: http://en.wikipedia.org/wiki/Mebibyte

        Example of a valid request (token is user specific):

        .. code-block:: http

            POST /api/openstack-tenants/c84d653b9ec92c6cbac41c706593e66f567a7fa4/set_quotas/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Host: example.com

            {
                "instances": 30,
                "ram": 100000,
                "storage": 1000000,
                "vcpu": 30,
                "security_group_count": 100,
                "security_group_rule_count": 100,
                "volumes": 10,
                "snapshots": 20
            }

        Response code of a successful request is **202 ACCEPTED**.
        In case tenant is in a non-stable status, the response would be **409 CONFLICT**.
        In this case REST client is advised to repeat the request after some time.
        On successful completion the task will synchronize quotas with the backend.
        """
        tenant = self.get_object()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        quotas = dict(serializer.validated_data)
        for quota_name, limit in quotas.items():
            tenant.set_quota_limit(quota_name, limit)
        executors.TenantPushQuotasExecutor.execute(tenant, quotas=quotas)

        return response.Response(
            {"detail": _("Quota update has been scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    set_quotas_permissions = [structure_permissions.is_staff]
    set_quotas_validators = [core_validators.StateValidator(models.Tenant.States.OK)]
    set_quotas_serializer_class = serializers.TenantQuotaSerializer

    @decorators.action(detail=True, methods=["post"])
    def create_network(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        network = serializer.save()

        executors.NetworkCreateExecutor().execute(network)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_network_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]
    create_network_serializer_class = serializers.NetworkSerializer

    def external_network_is_defined(tenant):
        if not tenant.external_network_id:
            raise core_exceptions.IncorrectStateException(
                _(
                    "Cannot create floating IP if tenant external network is not defined."
                )
            )

    @decorators.action(detail=True, methods=["post"])
    def create_floating_ip(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        floating_ip = serializer.save()

        executors.FloatingIPCreateExecutor.execute(floating_ip)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_floating_ip_validators = [
        core_validators.StateValidator(models.Tenant.States.OK),
        external_network_is_defined,
    ]
    create_floating_ip_serializer_class = serializers.FloatingIPSerializer

    @decorators.action(detail=True, methods=["post"])
    def pull_floating_ips(self, request, uuid=None):
        tenant = self.get_object()

        executors.TenantPullFloatingIPsExecutor.execute(tenant)
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_floating_ips_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]
    pull_floating_ips_serializer_class = rf_serializers.Serializer

    @decorators.action(detail=True, methods=["post"])
    def create_security_group(self, request, uuid=None):
        """
        Example of a request:

        .. code-block:: http

            {
                "name": "Security group name",
                "description": "description",
                "rules": [
                    {
                        "protocol": "tcp",
                        "from_port": 1,
                        "to_port": 10,
                        "cidr": "10.1.1.0/24"
                    },
                    {
                        "protocol": "udp",
                        "from_port": 10,
                        "to_port": 8000,
                        "cidr": "10.1.1.0/24"
                    }
                ]
            }
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        security_group = serializer.save()

        executors.SecurityGroupCreateExecutor().execute(security_group)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_security_group_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]
    create_security_group_serializer_class = serializers.SecurityGroupSerializer

    @decorators.action(detail=True, methods=["post"])
    def pull_security_groups(self, request, uuid=None):
        executors.TenantPullSecurityGroupsExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Security groups pull has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_security_groups_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]

    @decorators.action(detail=True, methods=["post"])
    def pull_server_groups(self, request, uuid=None):
        executors.TenantPullServerGroupsExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Server groups pull has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_server_groups_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]

    @decorators.action(detail=True, methods=["post"])
    def create_server_group(self, request, uuid=None):
        """
        Example of a request:

        .. code-block:: http

            {
                "name": "Server group name",
                "policy": "affinity"
            }
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        server_group = serializer.save()

        executors.ServerGroupCreateExecutor().execute(server_group)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_server_group_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]
    create_server_group_serializer_class = serializers.ServerGroupSerializer

    @decorators.action(detail=True, methods=["post"])
    def change_password(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.TenantChangeUserPasswordExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Password update has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    change_password_serializer_class = serializers.TenantChangePasswordSerializer
    change_password_validators = [
        core_validators.StateValidator(models.Tenant.States.OK)
    ]

    @decorators.action(detail=True, methods=["post"])
    def pull_quotas(self, request, uuid=None):
        executors.TenantPullQuotasExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Quotas pull has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_quotas_validators = [core_validators.StateValidator(models.Tenant.States.OK)]

    @decorators.action(detail=True, methods=["get"])
    def counters(self, request, uuid=None):
        from waldur_openstack.openstack_tenant import models as tenant_models

        tenant = self.get_object()
        service_settings = structure_models.ServiceSettings.objects.filter(
            scope=tenant
        ).first()
        return response.Response(
            {
                "instances": tenant_models.Instance.objects.filter(
                    service_settings=service_settings
                ).count(),
                "server_groups": tenant_models.ServerGroup.objects.filter(
                    settings=service_settings
                ).count(),
                "flavors": tenant_models.Flavor.objects.filter(
                    settings=service_settings
                ).count(),
                "images": tenant_models.Image.objects.filter(
                    settings=service_settings
                ).count(),
                "volumes": tenant_models.Volume.objects.filter(
                    service_settings=service_settings
                ).count(),
                "snapshots": tenant_models.Snapshot.objects.filter(
                    service_settings=service_settings
                ).count(),
                "networks": models.Network.objects.filter(tenant=tenant).count(),
                "floating_ips": models.FloatingIP.objects.filter(tenant=tenant).count(),
                "ports": models.Port.objects.filter(tenant=tenant).count(),
                "routers": models.Router.objects.filter(tenant=tenant).count(),
                "subnets": models.SubNet.objects.filter(network__tenant=tenant).count(),
                "security_groups": models.SecurityGroup.objects.filter(
                    tenant=tenant
                ).count(),
            }
        )


class RouterViewSet(core_views.ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    queryset = models.Router.objects.all().order_by("tenant__name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.RouterFilter
    serializer_class = serializers.RouterSerializer

    @decorators.action(detail=True, methods=["POST"])
    def set_routes(self, request, uuid=None):
        router = self.get_object()
        serializer = self.get_serializer(router, data=request.data)
        serializer.is_valid(raise_exception=True)
        old_routes = router.routes
        new_routes = serializer.validated_data["routes"]
        router.routes = new_routes
        router.save(update_fields=["routes"])
        executors.RouterSetRoutesExecutor().execute(router)

        event_logger.openstack_router.info(
            "Static routes have been updated.",
            event_type="openstack_router_updated",
            event_context={
                "router": router,
                "old_routes": old_routes,
                "new_routes": new_routes,
                "tenant_backend_id": router.tenant.backend_id,
            },
        )

        logger.info(
            "Static routes have been updated for router %s from %s to %s.",
            router,
            old_routes,
            new_routes,
        )

        return response.Response(
            {"status": _("Routes update was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    set_routes_serializer_class = serializers.RouterSetRoutesSerializer
    set_routes_validators = [
        core_validators.StateValidator(
            models.Router.States.OK, models.Router.States.ERRED
        )
    ]


class PortViewSet(structure_views.ResourceViewSet):
    queryset = models.Port.objects.all().order_by("network__name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.PortFilter
    serializer_class = serializers.PortSerializer

    disabled_actions = ["create", "update", "partial_update"]
    delete_executor = executors.PortDeleteExecutor


class NetworkViewSet(structure_views.ResourceViewSet):
    queryset = models.Network.objects.all().order_by("name")
    serializer_class = serializers.NetworkSerializer
    filterset_class = filters.NetworkFilter

    disabled_actions = ["create"]
    update_executor = executors.NetworkUpdateExecutor
    delete_executor = executors.NetworkDeleteExecutor
    pull_executor = executors.NetworkPullExecutor

    @decorators.action(detail=True, methods=["post"])
    def create_subnet(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subnet = serializer.save()
        executors.SubNetCreateExecutor.execute(subnet)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_subnet_validators = [
        core_validators.StateValidator(models.Network.States.OK)
    ]
    create_subnet_serializer_class = serializers.SubNetSerializer

    @decorators.action(detail=True, methods=["post"])
    def set_mtu(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        network = serializer.save()
        executors.SetMtuExecutor.execute(network)
        return response.Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    set_mtu_validators = [core_validators.StateValidator(models.Network.States.OK)]
    set_mtu_serializer_class = serializers.SetMtuSerializer

    @decorators.action(detail=True, methods=["post"])
    def create_port(self, request, uuid=None):
        network: models.Network = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        port: models.Port = serializer.save()

        executors.PortCreateExecutor().execute(
            port, network=core_utils.serialize_instance(network)
        )
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_port_serializer_class = serializers.PortSerializer

    create_port_validators = [core_validators.StateValidator(models.Network.States.OK)]


class SubNetViewSet(structure_views.ResourceViewSet):
    queryset = models.SubNet.objects.all().order_by("network")
    serializer_class = serializers.SubNetSerializer
    filterset_class = filters.SubNetFilter

    disabled_actions = ["create"]
    update_executor = executors.SubNetUpdateExecutor
    delete_executor = executors.SubNetDeleteExecutor
    pull_executor = executors.SubNetPullExecutor

    @decorators.action(detail=True, methods=["post"])
    def connect(self, request, uuid=None):
        executors.SubnetConnectExecutor.execute(self.get_object())
        return response.Response(status=status.HTTP_202_ACCEPTED)

    connect_validators = [core_validators.StateValidator(models.SubNet.States.OK)]
    connect_serializer_class = rf_serializers.Serializer

    @decorators.action(detail=True, methods=["post"])
    def disconnect(self, request, uuid=None):
        executors.SubnetDisconnectExecutor.execute(self.get_object())
        return response.Response(status=status.HTTP_202_ACCEPTED)

    disconnect_validators = [core_validators.StateValidator(models.SubNet.States.OK)]
    disconnect_serializer_class = rf_serializers.Serializer
