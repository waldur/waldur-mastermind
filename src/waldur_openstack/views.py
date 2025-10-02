import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Count,
)
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from keystoneauth1.exceptions.connection import ConnectFailure
from rest_framework import decorators, exceptions, generics, response, status

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import mixins as core_mixins
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates
from waldur_core.core.serializers import EmptySerializer
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import signals as structure_signals
from waldur_core.structure import views as structure_views
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.serializers import ConsoleUrlSerializer
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.marketplace_openstack.utils import delete_instance
from waldur_openstack.apps import OpenStackConfig
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.models import Instance, Network, Volume

from . import executors, filters, models, serializers, utils

logger = logging.getLogger(__name__)


class UsageReporter:
    """
    This class implements service for counting number of instances grouped
    by image and flavor name and by instance runtime status.
    Please note that even when flavors have different UUIDs they are treated
    as the same as long as they have the same name.
    This is needed because in OpenStack UUID is not stable for images and flavors.
    """

    def __init__(self, view, request):
        self.view = view
        self.request = request
        self.query = None

    def get_report(self):
        if self.request.query_params:
            self.query = self.parse_query(self.request)

        running_stats = self.get_stats(Instance.RuntimeStates.ACTIVE)
        created_stats = self.get_stats()
        qs = self.get_initial_queryset().values_list("name", flat=True).distinct()

        page = self.view.paginate_queryset(qs)
        result = self.serialize_result(page, running_stats, created_stats)
        return self.view.get_paginated_response(result)

    def serialize_result(self, queryset, running_stats, created_stats):
        result = []
        for name in queryset:
            result.append(
                {
                    "name": name,
                    "running_instances_count": running_stats.get(name, 0),
                    "created_instances_count": created_stats.get(name, 0),
                }
            )
        return result

    def apply_filters(self, qs):
        if self.query:
            filter_dict = dict()
            if self.query.get("shared", None):
                filter_dict["service_settings__shared"] = self.query["shared"]
            if self.query.get("service_provider", None):
                filter_dict["service_settings__uuid__in"] = self.query[
                    "service_provider"
                ]
                filter_dict["service_settings__type"] = "OpenStack"
            return qs.filter(**filter_dict)
        return qs

    def parse_query(self, request):
        serializer_class = serializers.OpenStackUsageStatsSerializer
        serializer = serializer_class(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data
        return query

    def get_initial_queryset(self):
        raise NotImplementedError

    def get_stats(self, runtime_state=None):
        raise NotImplementedError


class ImageUsageReporter(UsageReporter):
    def get_initial_queryset(self):
        return models.Image.objects.all()

    def get_stats(self, runtime_state=None):
        volumes = Volume.objects.filter(bootable=True)
        if runtime_state:
            volumes = volumes.filter(instance__runtime_state=runtime_state)
        rows = (
            self.apply_filters(volumes)
            .values("image_name")
            .annotate(count=Count("image_name"))
            .order_by()  # remove the extra group by arguments caused by default ordering
        )
        return {row["image_name"]: row["count"] for row in rows}


class FlavorUsageReporter(UsageReporter):
    def get_initial_queryset(self):
        return models.Flavor.objects.all()

    def get_stats(self, runtime_state=None):
        instances = Instance.objects.all()
        if runtime_state:
            instances = instances.filter(runtime_state=runtime_state)
        rows = (
            self.apply_filters(instances)
            .values("flavor_name")
            .annotate(count=Count("flavor_name"))
            .order_by()  # remove the extra group by arguments caused by default ordering
        )
        return {row["flavor_name"]: row["count"] for row in rows}


class FlavorViewSet(structure_views.BaseServicePropertyViewSet):
    """
    VM instance flavor is a pre-defined set of virtual hardware parameters that the instance will use:
    CPU, memory, disk size etc. VM instance flavor is not to be confused with VM template -- flavor is a set of virtual
    hardware parameters whereas template is a definition of a system to be installed on this instance.
    """

    queryset = models.Flavor.objects.all().order_by("settings", "cores", "ram", "disk")
    serializer_class = serializers.OpenStackFlavorSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.FlavorFilter

    @decorators.action(detail=False)
    def usage_stats(self, request):
        return FlavorUsageReporter(self, request).get_report()


class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all().order_by("name")
    serializer_class = serializers.OpenStackImageSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.ImageFilter

    @decorators.action(detail=False)
    def usage_stats(self, request):
        return ImageUsageReporter(self, request).get_report()


class VolumeTypeViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.VolumeType.objects.filter(disabled=False).order_by(
        "settings", "name"
    )
    serializer_class = serializers.OpenStackVolumeTypeSerializer
    lookup_field = "uuid"
    filterset_class = filters.VolumeTypeFilter

    @extend_schema(
        description="Return a list of unique volume type names.",
        responses=list[str],
    )
    @decorators.action(detail=False, methods=["get"])
    def names(self, request):
        names = (
            models.VolumeType.objects.filter(disabled=False)
            .values_list("name", flat=True)
            .distinct()
            .order_by("name")
        )
        return response.Response(names, status=status.HTTP_200_OK)


class SecurityGroupViewSet(structure_views.ResourceViewSet):
    queryset = models.SecurityGroup.objects.all().order_by("tenant__name")
    serializer_class = serializers.OpenStackSecurityGroupSerializer
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
        serializers.OpenStackSecurityGroupUpdateSerializer
    )

    destroy_validators = structure_views.ResourceViewSet.destroy_validators + [
        default_security_group_validator
    ]
    delete_executor = executors.SecurityGroupDeleteExecutor

    @extend_schema(
        description="Update security group rules",
        request=serializers.OpenStackSecurityGroupRuleListUpdateSerializer,
        responses=None,
        examples=[
            OpenApiExample(
                request_only=True,
                name="openstack-security-group-set-rules",
                value=[
                    {
                        "protocol": "tcp",
                        "from_port": 1,
                        "to_port": 10,
                        "cidr": "10.1.1.0/24",
                    }
                ],
            )
        ],
    )
    @decorators.action(detail=True, methods=["POST"])
    def set_rules(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        security_group: models.SecurityGroup = self.get_object()
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

    set_rules_validators = [core_validators.StateValidator(CoreStates.OK)]
    set_rules_serializer_class = (
        serializers.OpenStackSecurityGroupRuleListUpdateSerializer
    )


class ServerGroupViewSet(structure_views.ResourceViewSet):
    queryset = models.ServerGroup.objects.all().order_by("tenant__name")
    serializer_class = serializers.OpenStackServerGroupSerializer
    filterset_class = filters.ServerGroupFilter
    pull_executor = executors.ServerGroupPullExecutor
    delete_executor = executors.ServerGroupDeleteExecutor


class FloatingIPViewSet(structure_views.ResourceViewSet):
    queryset = models.FloatingIP.objects.all().order_by("address")
    serializer_class = serializers.OpenStackFloatingIPSerializer
    filterset_class = filters.FloatingIPFilter
    disabled_actions = ["update", "partial_update", "create"]
    delete_executor = executors.FloatingIPDeleteExecutor
    pull_executor = executors.FloatingIPPullExecutor

    def list(self, request, *args, **kwargs):
        """
        Status *DOWN* means that floating IP is not linked to a VM, status *ACTIVE* means that it is in use.
        """
        return super().list(request, *args, **kwargs)

    @extend_schema(
        description="Attach floating IP to port",
        request=serializers.OpenStackFloatingIPAttachSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_to_port(self, request, uuid=None):
        floating_ip: models.FloatingIP = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        port: models.Port = serializer.validated_data["port"]
        if port.state != CoreStates.OK:
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

    attach_to_port_serializer_class = serializers.OpenStackFloatingIPAttachSerializer
    attach_to_port_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="Detach floating IP from port",
        request=None,
        responses=None,
    )
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

    detach_from_port_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="Update description of the floating IP",
        request=serializers.OpenStackFloatingIPDescriptionUpdateSerializer,
        responses=None,
    )
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
        serializers.OpenStackFloatingIPDescriptionUpdateSerializer
    )
    update_description_validators = [core_validators.StateValidator(CoreStates.OK)]


class TenantViewSet(structure_views.ResourceViewSet):
    queryset = models.Tenant.objects.all().order_by("name")
    serializer_class = serializers.OpenStackTenantSerializer
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
    destroy_permissions = [delete_permission_check]

    @extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="openstack-tenant-set-quotas",
                value={
                    "instances": 30,
                    "ram": 100000,
                    "storage": 1000000,
                    "vcpu": 30,
                    "security_group_count": 100,
                    "security_group_rule_count": 100,
                    "volumes": 10,
                    "snapshots": 20,
                },
            )
        ]
    )
    @decorators.action(detail=True, methods=["post"])
    def set_quotas(self, request, uuid=None):
        """
        A quota can be set for a particular tenant. Only staff users can do that.
        In order to set quota submit POST request to /api/openstack-tenants/<uuid>/set_quotas/.
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

        Response code of a successful request is **202 ACCEPTED**.
        In case tenant is in a non-stable status, the response would be **409 CONFLICT**.
        In this case REST client is advised to repeat the request after some time.
        On successful completion the task will synchronize quotas with the backend.
        """
        tenant: models.Tenant = self.get_object()

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
    set_quotas_validators = [core_validators.StateValidator(CoreStates.OK)]
    set_quotas_serializer_class = serializers.OpenStackTenantQuotaSerializer

    @extend_schema(
        description="Create network for tenant",
    )
    @decorators.action(detail=True, methods=["post"])
    def create_network(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        network = serializer.save()

        executors.NetworkCreateExecutor().execute(network)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_network_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_network_serializer_class = serializers.OpenStackNetworkSerializer

    def external_network_is_defined(tenant):
        external_network_id = utils.get_external_network_id(tenant)
        if not external_network_id:
            raise core_exceptions.IncorrectStateException(
                _(
                    "Cannot create floating IP if tenant external network is not defined."
                )
            )

        # If we have external_network_id from settings but not on tenant, attempt recovery
        if external_network_id and not tenant.external_network_id:
            logger.info(
                "Attempting to recover external network for tenant %s before floating IP creation",
                tenant,
            )
            try:
                backend = OpenStackBackend(tenant.service_settings)
                backend.detect_external_network(tenant)
                tenant.refresh_from_db()
                # Check if recovery succeeded
                if not tenant.external_network_id:
                    logger.warning(
                        "External network recovery failed for tenant %s - network not set after recovery attempt",
                        tenant,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to recover external network for tenant %s: %s",
                    tenant,
                    e,
                )

    @extend_schema(
        description="Create floating IP for tenant",
        request=serializers.OpenStackFloatingIPSerializer,
        responses=serializers.OpenStackFloatingIPSerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def create_floating_ip(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        floating_ip = serializer.save()

        executors.FloatingIPCreateExecutor.execute(floating_ip)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_floating_ip_validators = [
        core_validators.StateValidator(CoreStates.OK),
        external_network_is_defined,
    ]
    create_floating_ip_serializer_class = serializers.OpenStackFloatingIPSerializer

    @extend_schema(
        description="Trigger job to pull floating IPs from remote VPC",
        request=None,
        responses={202: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def pull_floating_ips(self, request, uuid=None):
        tenant: models.Tenant = self.get_object()

        executors.TenantPullFloatingIPsExecutor.execute(tenant)
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_floating_ips_validators = [core_validators.StateValidator(CoreStates.OK)]
    pull_floating_ips_serializer_class = EmptySerializer

    @extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="openstack-tenant-create-security-group",
                description="Example of creating a security group with rules",
                value={
                    "name": "Security group name",
                    "description": "description",
                    "rules": [
                        {
                            "protocol": "tcp",
                            "from_port": 1,
                            "to_port": 10,
                            "cidr": "10.1.1.0/24",
                        },
                        {
                            "protocol": "udp",
                            "from_port": 10,
                            "to_port": 8000,
                            "cidr": "10.1.1.0/24",
                        },
                    ],
                },
            )
        ],
        responses={201: serializers.OpenStackSecurityGroupSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def create_security_group(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        security_group = serializer.save()

        executors.SecurityGroupCreateExecutor().execute(security_group)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_security_group_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_security_group_serializer_class = (
        serializers.OpenStackSecurityGroupSerializer
    )

    @extend_schema(
        description="Trigger job to pull security groups from remote VPC",
        request=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def pull_security_groups(self, request, uuid=None):
        executors.TenantPullSecurityGroupsExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Security groups pull has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_security_groups_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="Trigger job to pull server groups from remote VPC",
        request=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def pull_server_groups(self, request, uuid=None):
        executors.TenantPullServerGroupsExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Server groups pull has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_server_groups_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="openstack-tenant-create-server-group",
                value={"name": "Server group name", "policy": "affinity"},
            )
        ]
    )
    @decorators.action(detail=True, methods=["post"])
    def create_server_group(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        server_group = serializer.save()

        executors.ServerGroupCreateExecutor().execute(server_group)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_server_group_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_server_group_serializer_class = serializers.OpenStackServerGroupSerializer

    @extend_schema(
        description="Change password for tenant user",
        request=serializers.OpenStackTenantChangePasswordSerializer,
        responses=None,
    )
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

    change_password_serializer_class = (
        serializers.OpenStackTenantChangePasswordSerializer
    )
    change_password_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="It triggers celery job to pull quotas from remote VPC",
        request=None,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def pull_quotas(self, request, uuid=None):
        executors.TenantPullQuotasExecutor.execute(self.get_object())
        return response.Response(
            {"status": _("Quotas pull has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    pull_quotas_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="Return a list of volumes from backend",
        responses=serializers.OpenStackBackendInstanceSerializer(many=True),
        request=None,
    )
    @decorators.action(detail=True)
    def backend_instances(self, request, uuid=None):
        tenant: models.Tenant = self.get_object()
        backend = OpenStackBackend(tenant.service_settings)
        try:
            serializer = serializers.OpenStackBackendInstanceSerializer(
                backend.get_instances(tenant), many=True
            )
        except (ConnectFailure, OpenStackBackendError) as e:
            raise exceptions.ValidationError(e)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Return a list of volumes from backend",
        request=None,
        responses=serializers.OpenStackBackendVolumesSerializer(many=True),
    )
    @decorators.action(detail=True)
    def backend_volumes(self, request, uuid=None):
        tenant: models.Tenant = self.get_object()
        backend = OpenStackBackend(tenant.service_settings)
        try:
            serializer = serializers.OpenStackBackendVolumesSerializer(
                backend.get_volumes(tenant), many=True
            )
        except (ConnectFailure, OpenStackBackendError) as e:
            raise exceptions.ValidationError(e)
        return response.Response(serializer.data, status=status.HTTP_200_OK)


class RouterViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.Router.objects.all().order_by("tenant__name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.RouterFilter
    serializer_class = serializers.OpenStackRouterSerializer
    create_serializer_class = serializers.CreateRouterSerializer
    disabled_actions = ["update", "partial_update"]

    delete_executor = executors.RouterDeleteExecutor
    create_executor = executors.RouterCreateExecutor

    @decorators.action(detail=True, methods=["POST"])
    def set_routes(self, request, uuid=None):
        router: models.Router = self.get_object()
        serializer = self.get_serializer(router, data=request.data)
        serializer.is_valid(raise_exception=True)
        old_routes = router.routes
        new_routes = serializer.validated_data["routes"]
        router.routes = new_routes
        router.save(update_fields=["routes"])
        executors.RouterSetRoutesExecutor().execute(router)

        event_logger.emit(
            "Static routes have been updated.",
            event_type=EventType.OPENSTACK_ROUTER_UPDATED,
            event_context={
                "router": router,
                "old_routes": old_routes,
                "new_routes": new_routes,
                "tenant_backend_id": router.tenant.backend_id,
                "changed_interface": {},
            },
            scopes=[router, router.project, router.project.customer],
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

    set_routes_serializer_class = serializers.OpenStackRouterSetRoutesSerializer
    set_routes_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED)
    ]

    @extend_schema(
        description="Add interface to router. Either subnet or port must be provided.",
        request=serializers.OpenStackRouterInterfaceSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def add_router_interface(self, request, uuid=None):
        router: models.Router = self.get_object()
        serializer = serializers.OpenStackRouterInterfaceSerializer(
            data=request.data, context={"view": self}
        )
        serializer.is_valid(raise_exception=True)
        subnet = serializer.validated_data.get("subnet")
        port = serializer.validated_data.get("port")

        if port and port.device_owner:
            return response.Response(
                {
                    "port": f"Port cannot have an owner. Currently owner is {port.device_owner}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if port and port.status != "DOWN":
            return response.Response(
                {
                    "port": f"Port should be in DOWN status for attachment. Current status is {port.status}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_routes = router.routes
        backend: OpenStackBackend = router.tenant.get_backend()

        try:
            if not port:
                # If we pass only a subnet to router interface addition,
                # and some IPs in the subnet are already allocated (e.g., by other ports or as a gateway),
                # the operation may fail with an IP address conflict.
                # To avoid this, we first find a free IP in the subnet, create a port with this IP,
                # and then pass the port to the router interface addition.
                free_ip = backend.get_free_ip(subnet)
                if not free_ip:
                    return response.Response(
                        {
                            "status": _(
                                f"No available IP addresses in subnet {subnet.backend_id}."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                port = models.Port.objects.create(
                    subnet=subnet,
                    network=subnet.network,
                    tenant=subnet.tenant,
                    project=subnet.project,
                    service_settings=subnet.service_settings,
                    fixed_ips=[{"subnet_id": subnet.backend_id, "ip_address": free_ip}],
                )
                backend.create_port(port)
                logger.info(
                    f"Port {port.backend_id} with IP {free_ip} was created for router interface addition."
                )
            backend.add_router_interface(router, port=port)
        except OpenStackBackendError as e:
            return response.Response(
                {"status": _(f"Unable to add a new router interface: {e.args[0]}")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        added_interface = None
        if subnet:
            added_interface = {"type": "subnet", "backend_id": subnet.backend_id}
        elif port:
            added_interface = {"type": "port", "backend_id": port.backend_id}
        event_logger.emit(
            "Interface was added to router.",
            event_type=EventType.OPENSTACK_ROUTER_UPDATED,
            event_context={
                "router": router,
                "old_routes": old_routes,
                "new_routes": old_routes,  # routes are not changed, but for consistency
                "tenant_backend_id": router.tenant.backend_id,
                "changed_interface": added_interface,
            },
            scopes=[router, router.project, router.project.customer],
        )
        backend.pull_tenant_routers(router.tenant, router.backend_id)
        return response.Response(
            {
                "status": _(
                    f"Interface {added_interface} was added to router {router.backend_id}."
                )
            },
            status=status.HTTP_202_ACCEPTED,
        )

    add_router_interface_serializer_class = (
        serializers.OpenStackRouterInterfaceSerializer
    )
    add_router_interface_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="Remove interface from router. Either subnet or port must be provided.",
        request=serializers.OpenStackRouterInterfaceSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def remove_router_interface(self, request, uuid=None):
        router: models.Router = self.get_object()
        serializer = serializers.OpenStackRouterInterfaceSerializer(
            data=request.data, context={"view": self}
        )
        serializer.is_valid(raise_exception=True)
        subnet = serializer.validated_data.get("subnet")
        port = serializer.validated_data.get("port")
        executors.RouterInterfaceDeleteExecutor.execute(
            router,
            subnet_id=getattr(subnet, "id", None),
            port_id=getattr(port, "id", None),
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    remove_router_interface_serializer_class = (
        serializers.OpenStackRouterInterfaceSerializer
    )
    remove_router_interface_validators = [core_validators.StateValidator(CoreStates.OK)]


class PortViewSet(structure_views.ResourceViewSet):
    queryset = models.Port.objects.all().order_by("network__name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.PortFilter
    serializer_class = serializers.OpenStackPortSerializer

    create_executor = executors.PortCreateExecutor
    update_executor = executors.PortUpdateNameAndDescriptionExecutor
    delete_executor = executors.PortDeleteExecutor

    @extend_schema(
        description="Enable port security for the port",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def enable_port_security(self, request, uuid=None):
        port = self.get_object()
        backend = port.get_backend()
        backend.enable_port_security(port)

        port.port_security_enabled = True
        port.save(update_fields=["port_security_enabled"])

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        description="Disable port security for the port",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def disable_port_security(self, request, uuid=None):
        port = self.get_object()
        backend = port.get_backend()
        backend.disable_port_security(port)

        port.port_security_enabled = False
        port.security_groups.clear()  # Remove all security groups
        port.save(update_fields=["port_security_enabled"])

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        description="Enable port.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def enable_port(self, request, uuid=None):
        port = self.get_object()
        backend = port.get_backend()
        backend.enable_port(port)

        port.admin_state_up = True
        port.save(update_fields=["admin_state_up"])

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        description="Disable port.",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def disable_port(self, request, uuid=None):
        port = self.get_object()
        backend = port.get_backend()
        backend.disable_port(port)

        port.admin_state_up = False
        port.save(update_fields=["admin_state_up"])

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        description="Update port IP address.",
        request=serializers.OpenStackPortIPUpdateSerializer,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_port_ip(self, request, uuid=None):
        port = self.get_object()
        serializer = self.get_serializer(data=request.data, context={"port": port})
        serializer.is_valid(raise_exception=True)
        subnet = serializer.validated_data["subnet"]
        ip_address = serializer.validated_data["ip_address"]
        backend = port.get_backend()
        backend.update_port_ip(port, subnet.backend_id, ip_address)
        port.fixed_ips = [{"subnet_id": subnet.backend_id, "ip_address": ip_address}]
        port.save(update_fields=["fixed_ips"])
        return response.Response(status=status.HTTP_200_OK)

    update_port_ip_serializer_class = serializers.OpenStackPortIPUpdateSerializer

    @extend_schema(
        description="Update security groups of the port",
        request=serializers.OpenStackInstanceSecurityGroupsUpdateSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def update_security_groups(self, request, uuid=None):
        port: models.Port = self.get_object()
        serializer = self.get_serializer(port, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.PortUpdateSecurityGroupsExecutor().execute(port)
        return response.Response(
            {"status": _("security groups update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    def port_security_enabled(port):
        if not port.port_security_enabled:
            raise core_exceptions.IncorrectStateException(
                _("Port security must be enabled.")
            )

    update_security_groups_validators = [
        core_validators.StateValidator(CoreStates.OK),
        port_security_enabled,
    ]
    update_security_groups_serializer_class = (
        serializers.OpenStackInstanceSecurityGroupsUpdateSerializer
    )


class NetworkViewSet(structure_views.ResourceViewSet):
    queryset = Network.objects.all().order_by("name")
    serializer_class = serializers.OpenStackNetworkSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.NetworkFilter
    disabled_actions = ["create"]
    update_executor = executors.NetworkUpdateExecutor
    delete_executor = executors.NetworkDeleteExecutor
    pull_executor = executors.NetworkPullExecutor

    def action_permission_check(request, view, obj=None):
        if not obj:
            return

        if request.user.is_staff:
            return

        network = obj
        if not network.project.has_user(
            request.user
        ) and not network.project.customer.has_user(request.user):
            raise exceptions.PermissionDenied()

    update_permissions = destroy_permissions = set_mtu_permissions = (
        create_subnet_permissions
    ) = [action_permission_check]

    @staticmethod
    def get_related_networks(user):
        project_ids = UserRole.objects.filter(
            is_active=True,
            content_type=ContentType.objects.get_for_model(structure_models.Project),
            user_id=user.id,
        ).values_list("object_id", flat=True)

        customer_ids = UserRole.objects.filter(
            is_active=True,
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
            user_id=user.id,
        ).values_list("object_id", flat=True)
        org_project_ids = structure_models.Project.objects.filter(
            customer_id__in=customer_ids
        ).values_list("id", flat=True)

        all_project_ids = set(project_ids) | set(org_project_ids)

        own_networks = models.Network.objects.filter(project_id__in=all_project_ids)
        rbac_policies = models.NetworkRBACPolicy.objects.filter(
            target_tenant__project_id__in=all_project_ids
        ).values_list("network_id", flat=True)
        rbac_networks = models.Network.objects.filter(id__in=rbac_policies)
        return (own_networks | rbac_networks).distinct()

    def get_queryset(self):
        user: structure_models.User = self.request.user
        queryset = Network.objects.all().order_by("name")

        if user.is_staff or user.is_support:
            return queryset

        if not user.is_authenticated:
            return queryset.none()

        return NetworkViewSet.get_related_networks(user)

    @decorators.action(detail=True, methods=["post"])
    def create_subnet(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subnet = serializer.save()
        executors.SubNetCreateExecutor.execute(subnet)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_subnet_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_subnet_serializer_class = serializers.OpenStackSubNetSerializer

    @decorators.action(detail=True, methods=["post"])
    def set_mtu(self, request, uuid=None):
        serializer = self.get_serializer(instance=self.get_object(), data=request.data)
        serializer.is_valid(raise_exception=True)
        network: models.Network = serializer.save()
        executors.SetMtuExecutor.execute(network)
        return response.Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    set_mtu_validators = [core_validators.StateValidator(CoreStates.OK)]
    set_mtu_serializer_class = serializers.SetMtuSerializer

    # RBAC policy create/delete moved to standalone ViewSet: NetworkRBACPolicyViewSet
    # TODO: remove after 1.11.2025

    def _check_rbac_policy_permissions(self, user, network, target_tenant):
        if user.is_staff:
            return

        if (
            network.project.has_user(user, ProjectRole.ADMIN)
            or network.project.has_user(user, ProjectRole.MANAGER)
            or network.project.customer.has_user(user, CustomerRole.OWNER)
        ) and (
            target_tenant.project.has_user(user, ProjectRole.ADMIN)
            or target_tenant.project.has_user(user, ProjectRole.MANAGER)
            or target_tenant.project.customer.has_user(user, CustomerRole.OWNER)
        ):
            return

        raise exceptions.PermissionDenied()

    @extend_schema(
        description="Create RBAC policy for the network",
        request=serializers.DeprecatedNetworkRBACPolicySerializer,
        responses=serializers.DeprecatedNetworkRBACPolicySerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def rbac_policy_create(self, request, uuid=None):
        network: models.Network = self.get_object()
        serializer = self.get_serializer(
            data=request.data, context={"request": request, "network": network}
        )
        serializer.is_valid(raise_exception=True)

        target_tenant = serializer.validated_data["target_tenant"]
        policy_type = serializer.validated_data["policy_type"]

        self._check_rbac_policy_permissions(request.user, network, target_tenant)

        backend = network.tenant.get_backend()

        backend_id = backend.create_network_rbac_policy(
            network,
            target_tenant=target_tenant,
            policy_type=policy_type,
        )

        logger.info("RBAC policy created in backend with ID: %s", backend_id)

        policy = models.NetworkRBACPolicy.objects.create(
            network=network,
            target_tenant=target_tenant,
            backend_id=backend_id,
            policy_type=policy_type,
        )

        logger.info("RBAC policy record created in database with UUID: %s", policy.uuid)

        result_serializer = self.get_serializer(policy, context={"request": request})
        return response.Response(result_serializer.data, status=status.HTTP_201_CREATED)

    rbac_policy_create_validators = [core_validators.StateValidator(CoreStates.OK)]
    rbac_policy_create_serializer_class = (
        serializers.DeprecatedNetworkRBACPolicySerializer
    )

    @extend_schema(
        description="Delete RBAC policy for the network",
        request=None,
        responses={204: None},
        parameters=[
            OpenApiParameter(
                "rbac_policy_uuid",
                OpenApiTypes.UUID,
                OpenApiParameter.PATH,
                description="UUID of the RBAC policy to delete",
            )
        ],
    )
    @decorators.action(
        detail=True,
        methods=["delete"],
        url_path="rbac_policy_delete/(?P<rbac_policy_uuid>[^/.]+)",
    )
    def rbac_policy_delete(self, request, uuid=None, rbac_policy_uuid=None):
        network: models.Network = self.get_object()
        backend = network.tenant.get_backend()

        try:
            rbac_policy = models.NetworkRBACPolicy.objects.get(uuid=rbac_policy_uuid)
            self._check_rbac_policy_permissions(
                request.user, network, rbac_policy.target_tenant
            )
        except models.NetworkRBACPolicy.DoesNotExist:
            raise exceptions.NotFound(
                _("RBAC policy with backend ID %s does not exist.") % uuid
            )

        backend.delete_network_rbac_policy(
            rbac_id=rbac_policy.backend_id,
        )
        rbac_policy.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)


class SubNetViewSet(structure_views.ResourceViewSet):
    queryset = models.SubNet.objects.all().order_by("network")
    serializer_class = serializers.OpenStackSubNetSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.SubNetFilter

    disabled_actions = ["create"]
    update_executor = executors.SubNetUpdateExecutor
    delete_executor = executors.SubNetDeleteExecutor
    pull_executor = executors.SubNetPullExecutor

    def get_queryset(self):
        user: structure_models.User = self.request.user
        queryset = models.SubNet.objects.all().order_by("network")

        if user.is_staff or user.is_support:
            return queryset

        if not user.is_authenticated:
            return queryset.none()

        all_networks = NetworkViewSet.get_related_networks(user)
        return queryset.filter(network__in=all_networks)

    @decorators.action(detail=True, methods=["post"])
    def connect(self, request, uuid=None):
        executors.SubnetConnectExecutor.execute(self.get_object())
        return response.Response(status=status.HTTP_202_ACCEPTED)

    connect_validators = [core_validators.StateValidator(CoreStates.OK)]
    connect_serializer_class = EmptySerializer

    @decorators.action(detail=True, methods=["post"])
    def disconnect(self, request, uuid=None):
        executors.SubnetDisconnectExecutor.execute(self.get_object())
        return response.Response(status=status.HTTP_202_ACCEPTED)

    disconnect_validators = [core_validators.StateValidator(CoreStates.OK)]
    disconnect_serializer_class = EmptySerializer


class VolumeViewSet(structure_views.ResourceViewSet):
    queryset = models.Volume.objects.all().order_by("name")
    serializer_class = serializers.OpenStackVolumeSerializer
    filterset_class = filters.VolumeFilter

    update_executor = executors.VolumeUpdateExecutor
    pull_executor = executors.VolumePullExecutor
    disabled_actions = ["create", "destroy"]

    @staticmethod
    def _is_volume_bootable(volume):
        if volume.bootable:
            raise core_exceptions.IncorrectStateException(
                _("Volume cannot be bootable.")
            )

    @staticmethod
    def _is_volume_attached(volume):
        if not volume.instance:
            raise core_exceptions.IncorrectStateException(
                _("Volume is not attached to an instance.")
            )

    @extend_schema(
        description="Increase volume size",
        request=serializers.OpenStackVolumeExtendSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def extend(self, request, uuid=None):
        volume: models.Volume = self.get_object()
        old_size = volume.size
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        volume.refresh_from_db()
        executors.VolumeExtendExecutor().execute(
            volume, old_size=old_size, new_size=volume.size
        )

        return response.Response(
            {"status": _("extend was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _is_volume_instance_ok(volume):
        if volume.instance and volume.instance.state != CoreStates.OK:
            raise core_exceptions.IncorrectStateException(
                _("Volume instance should be in OK state.")
            )

    extend_validators = [
        utils.check_volume_resize_enabled,
        _is_volume_instance_ok,
        core_validators.StateValidator(CoreStates.OK),
    ]
    extend_serializer_class = serializers.OpenStackVolumeExtendSerializer

    @extend_schema(
        description="Create snapshot from volume",
        request=serializers.OpenStackSnapshotSerializer,
        responses={201: serializers.OpenStackSnapshotSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def snapshot(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        snapshot = serializer.save()

        executors.SnapshotCreateExecutor().execute(snapshot)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    snapshot_serializer_class = serializers.OpenStackSnapshotSerializer

    @extend_schema(
        description="Attach volume to instance",
        request=serializers.VolumeAttachSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def attach(self, request, uuid=None):
        volume: models.Volume = self.get_object()
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.VolumeAttachExecutor().execute(volume)
        return response.Response(
            {"status": _("attach was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    attach_validators = [
        core_validators.RuntimeStateValidator("available"),
        core_validators.StateValidator(CoreStates.OK),
    ]
    attach_serializer_class = serializers.VolumeAttachSerializer

    @extend_schema(
        description="Detach instance from volume",
        request=None,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def detach(self, request, uuid=None):
        volume: models.Volume = self.get_object()
        executors.VolumeDetachExecutor().execute(volume)
        return response.Response(
            {"status": _("detach was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    detach_validators = [
        _is_volume_bootable,
        _is_volume_attached,
        core_validators.RuntimeStateValidator("in-use"),
        core_validators.StateValidator(CoreStates.OK),
    ]

    @extend_schema(
        description="Retype detached volume",
        request=serializers.OpenStackVolumeRetypeSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def retype(self, request, uuid=None):
        volume: models.Volume = self.get_object()
        serializer = self.get_serializer(volume, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.VolumeRetypeExecutor().execute(volume)
        return response.Response(
            {"status": _("retype was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    retype_validators = [
        core_validators.RuntimeStateValidator("available"),
        core_validators.StateValidator(CoreStates.OK),
    ]

    retype_serializer_class = serializers.OpenStackVolumeRetypeSerializer


class SnapshotViewSet(structure_views.ResourceViewSet):
    queryset = models.Snapshot.objects.all().order_by("name")
    serializer_class = serializers.OpenStackSnapshotSerializer
    update_executor = executors.SnapshotUpdateExecutor
    delete_executor = executors.SnapshotDeleteExecutor
    pull_executor = executors.SnapshotPullExecutor
    filterset_class = filters.SnapshotFilter
    disabled_actions = ["create"]

    @extend_schema(
        description="Restore volume from snapshot",
        request=serializers.OpenStackSnapshotRestorationSerializer,
        responses=serializers.OpenStackVolumeSerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        restoration = serializer.save()

        executors.SnapshotRestorationExecutor().execute(restoration)
        serialized_volume = serializers.OpenStackVolumeSerializer(
            restoration.volume, context={"request": self.request}
        )
        resource_imported.send(
            sender=models.Volume,
            instance=restoration.volume,
        )
        return response.Response(serialized_volume.data, status=status.HTTP_201_CREATED)

    restore_serializer_class = serializers.OpenStackSnapshotRestorationSerializer
    restore_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        description="Get a list of snapshot restorations",
        request=None,
        responses=serializers.OpenStackSnapshotRestorationSerializer(many=True),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def restorations(self, request, uuid=None):
        snapshot: models.Snapshot = self.get_object()
        serializer = self.get_serializer(snapshot.restorations.all(), many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    restorations_serializer_class = serializers.OpenStackSnapshotRestorationSerializer


class InstanceAvailabilityZoneViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.InstanceAvailabilityZone.objects.all().order_by(
        "settings", "name"
    )
    serializer_class = serializers.OpenStackInstanceAvailabilityZoneSerializer
    lookup_field = "uuid"
    filterset_class = filters.InstanceAvailabilityZoneFilter


class InstanceViewSet(structure_views.ResourceViewSet):
    """
    OpenStack instance permissions
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

    - Staff members can list all available VM instances in any service.
    - Customer owners can list all VM instances in all the services that belong to any of the customers they own.
    - Project administrators can list all VM instances, create new instances and start/stop/restart instances in all the
      services that are connected to any of the projects they are administrators in.
    - Project managers can list all VM instances in all the services that are connected to any of the projects they are
      managers in.
    """

    queryset = models.Instance.objects.all()
    serializer_class = serializers.OpenStackInstanceSerializer
    filterset_class = filters.InstanceFilter
    filter_backends = structure_views.ResourceViewSet.filter_backends + (
        structure_filters.StartTimeFilter,
    )
    pull_executor = executors.InstancePullExecutor

    update_executor = executors.InstanceUpdateExecutor
    update_validators = partial_update_validators = [
        core_validators.StateValidator(CoreStates.OK)
    ]
    disabled_actions = ["create", "destroy"]

    def _has_backups(instance):
        if instance.backups.exists():
            raise core_exceptions.IncorrectStateException(
                _("Cannot delete instance that has backups.")
            )

    def _has_snapshots(instance):
        for volume in instance.volumes.all():
            if volume.snapshots.exists():
                raise core_exceptions.IncorrectStateException(
                    _("Cannot delete instance that has snapshots.")
                )

    @extend_schema(
        description="Change flavor of the instance",
        request=serializers.InstanceFlavorChangeSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def change_flavor(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        old_flavor_name = instance.flavor_name
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        flavor = serializer.validated_data.get("flavor")
        executors.InstanceFlavorChangeExecutor().execute(
            instance, flavor=flavor, old_flavor_name=old_flavor_name
        )
        return response.Response(
            {"status": _("change_flavor was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    def _can_change_flavor(instance):
        if (
            instance.state == CoreStates.OK
            and instance.runtime_state == models.Instance.RuntimeStates.ACTIVE
        ):
            raise core_exceptions.IncorrectStateException(
                _("Please stop the instance before changing its flavor.")
            )

    change_flavor_serializer_class = serializers.InstanceFlavorChangeSerializer
    change_flavor_validators = [
        _can_change_flavor,
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.SHUTOFF),
    ]

    @extend_schema(
        description="Start the instance",
        request=None,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def start(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        executors.InstanceStartExecutor().execute(instance)
        return response.Response(
            {"status": _("start was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _can_start_instance(instance):
        if (
            instance.state == CoreStates.OK
            and instance.runtime_state == models.Instance.RuntimeStates.ACTIVE
        ):
            raise core_exceptions.IncorrectStateException(
                _("Instance is already active.")
            )

    start_validators = [
        _can_start_instance,
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.SHUTOFF),
    ]
    start_serializer_class = EmptySerializer

    @extend_schema(
        description="Stop the instance",
        request=None,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def stop(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        executors.InstanceStopExecutor().execute(instance)
        return response.Response(
            {"status": _("stop was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _can_stop_instance(instance):
        if (
            instance.state == CoreStates.OK
            and instance.runtime_state == models.Instance.RuntimeStates.SHUTOFF
        ):
            raise core_exceptions.IncorrectStateException(
                _("Instance is already stopped.")
            )

    stop_validators = [
        _can_stop_instance,
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.ACTIVE),
    ]
    stop_serializer_class = EmptySerializer

    @extend_schema(
        description="Restart the instance",
        request=None,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def restart(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        executors.InstanceRestartExecutor().execute(instance)
        return response.Response(
            {"status": _("restart was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    def _can_restart_instance(instance):
        if (
            instance.state == CoreStates.OK
            and instance.runtime_state == models.Instance.RuntimeStates.SHUTOFF
        ):
            raise core_exceptions.IncorrectStateException(
                _("Please start instance first.")
            )

    restart_validators = [
        _can_restart_instance,
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.ACTIVE),
    ]
    restart_serializer_class = EmptySerializer

    @extend_schema(
        description="Update security groups of the instance",
        request=serializers.OpenStackInstanceSecurityGroupsUpdateSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def update_security_groups(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.InstanceUpdateSecurityGroupsExecutor().execute(instance)
        return response.Response(
            {"status": _("security groups update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_security_groups_validators = [core_validators.StateValidator(CoreStates.OK)]
    update_security_groups_serializer_class = (
        serializers.OpenStackInstanceSecurityGroupsUpdateSerializer
    )

    @extend_schema(
        description="Create backup from instance",
        request=serializers.OpenStackBackupSerializer,
        responses=serializers.OpenStackBackupSerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def backup(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        backup = serializer.save()

        executors.BackupCreateExecutor().execute(backup)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    backup_validators = [core_validators.StateValidator(CoreStates.OK)]
    backup_serializer_class = serializers.OpenStackBackupSerializer

    @extend_schema(
        description="Update allowed address pairs of the instance",
        request=serializers.OpenStackInstanceAllowedAddressPairsUpdateSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def update_allowed_address_pairs(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        subnet = serializer.validated_data["subnet"]
        allowed_address_pairs = serializer.validated_data["allowed_address_pairs"]
        try:
            port = models.Port.objects.get(instance=instance, subnet=subnet)
        except models.Port.DoesNotExist:
            return response.Response(
                {"status": _("Port is not found.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except models.Port.MultipleObjectsReturned:
            return response.Response(
                {"status": _("Multiple ports are found.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        executors.InstanceAllowedAddressPairsUpdateExecutor().execute(
            instance,
            backend_id=port.backend_id,
            allowed_address_pairs=allowed_address_pairs,
        )
        return response.Response(
            {"status": _("Allowed address pairs update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_allowed_address_pairs_validators = [
        core_validators.StateValidator(CoreStates.OK)
    ]
    update_allowed_address_pairs_serializer_class = (
        serializers.OpenStackInstanceAllowedAddressPairsUpdateSerializer
    )

    @extend_schema(
        description="Update ports of the instance",
        request=serializers.OpenStackInstancePortsUpdateSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def update_ports(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.InstancePortsUpdateExecutor().execute(instance)
        return response.Response(
            {"status": _("internal ips update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_ports_validators = [core_validators.StateValidator(CoreStates.OK)]
    update_ports_serializer_class = serializers.OpenStackInstancePortsUpdateSerializer

    @extend_schema(
        description="Get a list of instance ports",
        request=None,
        responses=serializers.OpenStackNestedPortSerializer(many=True),
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def ports(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance.ports.all(), many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    ports_serializer_class = serializers.OpenStackNestedPortSerializer

    @extend_schema(
        description="Update floating IPs of the instance",
        request=serializers.OpenStackInstanceFloatingIPsUpdateSerializer,
        responses=None,
    )
    @decorators.action(detail=True, methods=["post"])
    def update_floating_ips(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.InstanceFloatingIPsUpdateExecutor().execute(instance)
        return response.Response(
            {"status": _("Floating IPs update was scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_floating_ips_validators = [core_validators.StateValidator(CoreStates.OK)]
    update_floating_ips_serializer_class = (
        serializers.OpenStackInstanceFloatingIPsUpdateSerializer
    )

    @extend_schema(
        description="Get a list of instance floating IPs",
        request=None,
        responses=serializers.OpenStackInstanceFloatingIpsSerializer,
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def floating_ips(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = serializers.OpenStackNestedFloatingIPSerializer(
            instance=instance.floating_ips.all(),
            many=True,
            context=self.get_serializer_context(),
        )
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Get console url for the instance",
        request=None,
        responses=ConsoleUrlSerializer,
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def console(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        backend = instance.get_backend()
        try:
            url = backend.get_console_url(instance)
        except OpenStackBackendError as e:
            raise exceptions.ValidationError(str(e))

        return response.Response({"url": url}, status=status.HTTP_200_OK)

    console_validators = [core_validators.StateValidator(CoreStates.OK)]

    def check_permissions_for_console(request, view, instance=None):
        if not instance:
            return

        if request.user.is_staff:
            return

        if settings.WALDUR_OPENSTACK["ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS"]:
            structure_permissions.is_administrator(request, view, instance)
        else:
            raise exceptions.PermissionDenied()

    console_permissions = [check_permissions_for_console]

    @extend_schema(
        description="Get console log for the instance",
        parameters=[OpenApiParameter("length", int, OpenApiParameter.QUERY)],
        request=None,
        responses={200: str},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def console_log(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        backend = instance.get_backend()
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        length = serializer.validated_data.get("length")

        try:
            log = backend.get_console_output(instance, length)
        except OpenStackBackendError as e:
            raise exceptions.ValidationError(str(e))

        return response.Response(log, status=status.HTTP_200_OK)

    console_log_serializer_class = serializers.OpenStackConsoleLogSerializer
    console_log_permissions = [structure_permissions.is_administrator]


class MarketplaceInstanceViewSet(structure_views.ResourceViewSet):
    queryset = models.Instance.objects.all()
    serializer_class = serializers.OpenStackInstanceCreateSerializer
    filter_backends = structure_views.ResourceViewSet.filter_backends + (
        structure_filters.StartTimeFilter,
    )

    @decorators.action(detail=True, methods=["delete"])
    def force_destroy(self, request, uuid=None):
        """This action completely repeats 'destroy', with the exclusion of validators.
        Destroy's validators require stopped VM. This requirement has expired.
        But for compatibility with old documentation, it must be left.
        """
        instance = self.get_object()
        delete_instance(instance, request.query_params)
        return response.Response(status=status.HTTP_202_ACCEPTED)

    force_destroy_validators = [
        InstanceViewSet._has_backups,
        InstanceViewSet._has_snapshots,
        core_validators.StateValidator(
            CoreStates.OK,
            CoreStates.ERRED,
        ),
    ]

    def perform_create(self, serializer):
        instance: models.Instance = serializer.save()
        executors.InstanceCreateExecutor.execute(
            instance,
            ssh_key=serializer.validated_data.get("ssh_public_key"),
            flavor=serializer.validated_data["flavor"],
            server_group=serializer.validated_data.get("server_group"),
            is_heavy_task=True,
        )


class MarketplaceVolumeViewSet(structure_views.ResourceViewSet):
    queryset = models.Volume.objects.all().order_by("name")
    serializer_class = serializers.OpenStackVolumeSerializer
    filterset_class = filters.VolumeFilter

    create_executor = executors.VolumeCreateExecutor

    def _can_destroy_volume(volume):
        if volume.state == CoreStates.ERRED:
            return
        if volume.state != CoreStates.OK:
            raise core_exceptions.IncorrectStateException(
                _("Volume should be in OK state.")
            )
        core_validators.RuntimeStateValidator(
            "available", "error", "error_restoring", "error_extending", ""
        )(volume)

    def _volume_snapshots_exist(volume):
        if volume.snapshots.exists():
            raise core_exceptions.IncorrectStateException(
                _("Volume has dependent snapshots.")
            )

    delete_executor = executors.VolumeDeleteExecutor
    destroy_validators = [
        _can_destroy_volume,
        _volume_snapshots_exist,
    ]


class BackupViewSet(structure_views.ResourceViewSet):
    queryset = models.Backup.objects.all().order_by("name")
    serializer_class = serializers.OpenStackBackupSerializer
    filterset_class = filters.BackupFilter
    disabled_actions = ["create"]

    delete_executor = executors.BackupDeleteExecutor

    # method has to be overridden in order to avoid triggering of UpdateExecutor
    # which is a default action for all ResourceViewSet(s)
    def perform_update(self, serializer):
        serializer.save()

    @extend_schema(
        description="Restore instance from backup",
        request=serializers.OpenStackBackupRestorationCreateSerializer,
        responses=serializers.OpenStackInstanceSerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def restore(self, request, uuid=None):
        instance: models.Backup = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        backup_restoration = serializer.save()

        # Note that connected volumes will be linked with new marketplace.Resources by handler in openstack_marketplace
        structure_signals.resource_imported.send(
            sender=models.Instance,
            instance=backup_restoration.instance,
        )

        # It is assumed that SSH public key is already stored in OpenStack system volume.
        # Therefore we don't need to specify it explicitly for cloud init service.
        executors.InstanceCreateExecutor.execute(
            backup_restoration.instance,
            flavor=backup_restoration.flavor,
            is_heavy_task=True,
        )

        instance_serializer = serializers.OpenStackInstanceSerializer(
            backup_restoration.instance, context={"request": self.request}
        )
        return response.Response(
            instance_serializer.data, status=status.HTTP_201_CREATED
        )

    restore_validators = [core_validators.StateValidator(CoreStates.OK)]
    restore_serializer_class = serializers.OpenStackBackupRestorationCreateSerializer


class SharedSettingsBaseView(generics.GenericAPIView):
    def get_tenants(self):
        service_settings_uuid = self.request.query_params.get("service_settings_uuid")
        if not service_settings_uuid or not core_utils.is_uuid_like(
            service_settings_uuid
        ):
            return structure_models.ServiceSettings.objects.none()

        queryset = structure_models.ServiceSettings.objects.filter(
            type=OpenStackConfig.service_name
        )
        queryset = filter_queryset_for_user(queryset, self.request.user)
        try:
            shared_settings = queryset.get(uuid=service_settings_uuid)
        except structure_models.ServiceSettings.DoesNotExist:
            return structure_models.ServiceSettings.objects.none()

        tenants = models.Tenant.objects.filter(service_settings=shared_settings)
        tenants = filter_queryset_for_user(tenants, self.request.user)
        return tenants

    def get(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)


class VolumeAvailabilityZoneViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.VolumeAvailabilityZone.objects.all().order_by("settings", "name")
    serializer_class = serializers.OpenStackVolumeAvailabilityZoneSerializer
    lookup_field = "uuid"
    filterset_class = filters.VolumeAvailabilityZoneFilter


class NetworkRBACPolicyViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.NetworkRBACPolicy.objects.all().order_by("-created")
    serializer_class = serializers.NetworkRBACPolicySerializer
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.NetworkRBACPolicyFilter

    def get_queryset(self):
        return filter_queryset_for_user(self.queryset, self.request.user)

    def _check_rbac_policy_permissions(self, user, network, target_tenant):
        if user.is_staff:
            return

        if (
            network.project.has_user(user, ProjectRole.ADMIN)
            or network.project.has_user(user, ProjectRole.MANAGER)
            or network.project.customer.has_user(user, CustomerRole.OWNER)
        ) and (
            target_tenant.project.has_user(user, ProjectRole.ADMIN)
            or target_tenant.project.has_user(user, ProjectRole.MANAGER)
            or target_tenant.project.customer.has_user(user, CustomerRole.OWNER)
        ):
            return

        raise exceptions.PermissionDenied()

    @extend_schema(
        description="Create RBAC policy for the network",
        request=serializers.NetworkRBACPolicySerializer,
        responses=serializers.NetworkRBACPolicySerializer,
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        network = serializer.validated_data["network"]
        target_tenant = serializer.validated_data["target_tenant"]
        policy_type = serializer.validated_data["policy_type"]

        self._check_rbac_policy_permissions(request.user, network, target_tenant)

        backend = network.tenant.get_backend()

        backend_id = backend.create_network_rbac_policy(
            network,
            target_tenant=target_tenant,
            policy_type=policy_type,
        )

        logger.info("RBAC policy created in backend with ID: %s", backend_id)

        policy = models.NetworkRBACPolicy.objects.create(
            network=network,
            target_tenant=target_tenant,
            backend_id=backend_id,
            policy_type=policy_type,
        )

        logger.info("RBAC policy record created in database with UUID: %s", policy.uuid)

        result_serializer = self.get_serializer(policy, context={"request": request})
        return response.Response(result_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        description="Delete RBAC policy for the network",
        request=None,
        responses={204: None},
    )
    def destroy(self, request, *args, **kwargs):
        policy: models.NetworkRBACPolicy = self.get_object()

        self._check_rbac_policy_permissions(
            request.user, policy.network, policy.target_tenant
        )

        backend = policy.network.tenant.get_backend()
        backend.delete_network_rbac_policy(rbac_id=policy.backend_id)
        policy.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)
