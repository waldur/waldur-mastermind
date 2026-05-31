from rest_framework import status
from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes
import logging

from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Count,
    Q,
    Sum,
)
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from keystoneauth1.exceptions.connection import ConnectFailure
from rest_framework import decorators, exceptions, generics, response, status
from rest_framework import serializers as rf_serializers

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import mixins as core_mixins
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core.serializers import StatusSerializer, DetailSerializer
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates
from waldur_core.logging import event_logger
from waldur_core.logging.diff import compute_collection_diff
from waldur_core.logging.enums import EventType
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import signals as structure_signals
from waldur_core.structure import views as structure_views
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.serializers import ConsoleUrlSerializer
from waldur_core.structure.signals import resource_imported
from waldur_mastermind.marketplace_openstack.utils import delete_instance
from waldur_openstack import routes, topology
from waldur_openstack.apps import OpenStackConfig
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.models import Instance, Network, Volume

from . import audit, executors, filters, models, serializers, utils
from . import permissions as openstack_permissions

logger = logging.getLogger(__name__)


class LBaaSAuditMixin:
    """Emit lifecycle audit events for LBaaS resources on create/update/delete.

    Designed to be mixed into ViewSets that also use ExecutorMixin. The events
    fire from the API request thread, so they carry actor context (user, IP,
    request id) auto-attached by CaptureEventContextMiddleware.
    """

    def perform_create(self, serializer):
        super().perform_create(serializer)
        instance = serializer.instance
        if instance is not None:
            audit.emit_lbaas_lifecycle_event(instance, "created")

    def perform_update(self, serializer):
        instance = self.get_object()
        _, serialize, _scope = audit._LBAAS_AUDIT_CONFIG[type(instance)]
        old_payload = serialize(instance)
        super().perform_update(serializer)
        instance.refresh_from_db()
        audit.emit_lbaas_lifecycle_event(instance, "updated", old_payload=old_payload)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        audit.emit_lbaas_lifecycle_event(instance, "deleted")
        return super().destroy(request, *args, **kwargs)


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


@extend_schema_view(
    list=extend_schema(
        summary="List flavors",
        description="Get a list of available VM instance flavors.",
    ),
    retrieve=extend_schema(
        summary="Get flavor details",
        description="Retrieve details of a specific VM instance flavor.",
    ),
)
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

    @extend_schema(
        summary="Get flavor usage statistics",
        description="Retrieve usage statistics for VM instance flavors, showing running and created instance counts for each flavor.",
        responses={status.HTTP_200_OK: OpenApiTypes.OBJECT},
    )
    @decorators.action(detail=False)
    def usage_stats(self, request):
        return FlavorUsageReporter(self, request).get_report()


@extend_schema_view(
    list=extend_schema(
        summary="List images",
        description="Get a list of available VM instance images.",
    ),
    retrieve=extend_schema(
        summary="Get image details",
        description="Retrieve details of a specific VM instance image.",
    ),
)
class ImageViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Image.objects.all().order_by("name")
    serializer_class = serializers.OpenStackImageSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.ImageFilter

    @extend_schema(
        summary="Get image usage statistics",
        description="Retrieve usage statistics for VM instance images, showing running and created instance counts for each image.",
        responses={
            status.HTTP_200_OK: serializers.OpenStackUsageStatsResponseSerializer
        },
    )
    @decorators.action(detail=False)
    def usage_stats(self, request):
        return ImageUsageReporter(self, request).get_report()


@extend_schema_view(
    list=extend_schema(
        summary="List volume types",
        description="Get a list of available volume types.",
    ),
    retrieve=extend_schema(
        summary="Get volume type details",
        description="Retrieve details of a specific volume type.",
    ),
)
class VolumeTypeViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.VolumeType.objects.filter(disabled=False).order_by(
        "settings", "name"
    )
    serializer_class = serializers.OpenStackVolumeTypeSerializer
    lookup_field = "uuid"
    filterset_class = filters.VolumeTypeFilter

    @extend_schema(
        summary="List unique volume type names",
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


@extend_schema_view(
    list=extend_schema(
        summary="List external networks",
        description="Get a list of provider-level external networks discovered from OpenStack.",
    ),
    retrieve=extend_schema(
        summary="Get external network details",
        description="Retrieve details of a specific external network, including its subnets.",
    ),
)
class ExternalNetworkViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.ExternalNetwork.objects.all().order_by("settings", "name")
    serializer_class = serializers.ExternalNetworkSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ExternalNetworkFilter


class HypervisorViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Hypervisor.objects.all().order_by("settings", "name")
    serializer_class = serializers.HypervisorSerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.HypervisorFilter

    @extend_schema(
        summary="Get hypervisor summary statistics",
        description=(
            "Return aggregated vCPU, RAM and disk totals across all hypervisors "
            "matching the current filter (e.g. settings_uuid)."
        ),
        parameters=[
            OpenApiParameter(
                "settings_uuid",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                required=True,
                description="UUID of the OpenStack ServiceSettings to aggregate over.",
            ),
        ],
        responses={200: serializers.HypervisorSummarySerializer},
    )
    @decorators.action(detail=False, methods=["get"])
    def summary(self, request):
        if not request.query_params.get("settings_uuid"):
            raise exceptions.ValidationError(
                {"settings_uuid": "This parameter is required."}
            )
        qs = self.filter_queryset(self.get_queryset())
        result = qs.aggregate(
            total_vcpus=Sum("vcpus"),
            used_vcpus=Sum("vcpus_used"),
            total_memory_mb=Sum("memory_mb"),
            used_memory_mb=Sum("memory_mb_used"),
            total_local_gb=Sum("local_gb"),
            used_local_gb=Sum("local_gb_used"),
            total_running_vms=Sum("running_vms"),
        )
        # total_vcpus already contains the effective (overcommit-applied)
        # number per host, sourced from Placement's per-RP allocation_ratio.
        # See pull_hypervisors / _collect_placement_capacity in backend.py.
        result = {k: v or 0 for k, v in result.items()}
        serializer = serializers.HypervisorSummarySerializer(result)
        return response.Response(serializer.data)

    @extend_schema(
        summary="Pre-flight allocation candidates",
        description=(
            "Ask Placement which compute hosts could currently satisfy a "
            "request for the given resources (and required traits). Useful "
            "as a pre-flight check before placing an order on a fully-booked "
            "cloud. Returns 0 candidates when nothing fits, with the same "
            "provider_summaries Placement returns for diagnostic display."
        ),
        parameters=[
            OpenApiParameter(
                "settings_uuid",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                required=True,
            ),
            OpenApiParameter(
                "resources",
                str,
                OpenApiParameter.QUERY,
                required=True,
                description="e.g. VCPU:4,MEMORY_MB:8192,DISK_GB:10",
            ),
            OpenApiParameter(
                "required",
                str,
                OpenApiParameter.QUERY,
                required=False,
                description="e.g. HW_CPU_X86_AVX2,STORAGE_DISK_SSD",
            ),
            OpenApiParameter(
                "limit",
                int,
                OpenApiParameter.QUERY,
                required=False,
                description="Cap on returned candidates (default 10).",
            ),
        ],
        responses={200: serializers.AllocationCandidatesResponseSerializer},
    )
    @decorators.action(detail=False, methods=["get"])
    def allocation_candidates(self, request):
        query = serializers.AllocationCandidatesQuerySerializer(
            data=request.query_params
        )
        query.is_valid(raise_exception=True)
        # Permission scoping: confirm the caller can see at least one
        # hypervisor for that settings_uuid (relies on the same
        # GenericRoleFilter the queryset already uses).
        settings_uuid = query.validated_data["settings_uuid"].hex
        accessible_qs = self.filter_queryset(self.get_queryset()).filter(
            settings__uuid=settings_uuid
        )
        if not accessible_qs.exists():
            raise exceptions.PermissionDenied(
                "No accessible hypervisors for the given settings_uuid."
            )
        settings = accessible_qs.first().settings

        resources = serializers.AllocationCandidatesQuerySerializer.parse_resources(
            query.validated_data["resources"]
        )
        required_str = query.validated_data.get("required") or ""
        required = [t.strip() for t in required_str.split(",") if t.strip()]

        backend = OpenStackBackend(settings)
        try:
            raw = backend.get_allocation_candidates(
                resources=resources,
                required=required or None,
                limit=query.validated_data.get("limit"),
            )
        except OpenStackBackendError as e:
            raise exceptions.ValidationError(str(e))

        result = {
            "candidate_count": len(raw.get("allocation_requests", [])),
            "provider_summaries": raw.get("provider_summaries", {}),
        }
        out = serializers.AllocationCandidatesResponseSerializer(result)
        return response.Response(out.data)


class HypervisorInventoryViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.HypervisorInventory.objects.all().order_by(
        "hypervisor", "resource_class"
    )
    serializer_class = serializers.HypervisorInventorySerializer
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.HypervisorInventoryFilter


@extend_schema_view(
    list=extend_schema(
        summary="List security groups",
        description="Get a list of security groups.",
    ),
    retrieve=extend_schema(
        summary="Get security group details",
        description="Retrieve details of a specific security group.",
    ),
    partial_update=extend_schema(
        summary="Partially update security group",
        description="Update specific fields of a security group.",
    ),
    destroy=extend_schema(
        summary="Delete security group",
        description="Delete a security group.",
    ),
)
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
        summary="Set security group rules",
        description="Update the rules for a specific security group. This overwrites all existing rules.",
        request=serializers.OpenStackSecurityGroupRuleListUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        old_snapshot = audit.snapshot_security_group_rules(security_group)

        serializer.save()
        security_group.refresh_from_db()

        new_snapshot = audit.snapshot_security_group_rules(security_group)
        diff = compute_collection_diff(
            old_snapshot,
            new_snapshot,
            identity_key=lambda r: r["_pk"],
            compare_fields=audit.SECURITY_GROUP_RULE_COMPARE_FIELDS,
            serialize=lambda r: {k: v for k, v in r.items() if k != "_pk"},
        )
        audit.emit_security_group_rules_changed(
            security_group, diff, trigger="user_action"
        )

        executors.PushSecurityGroupRulesExecutor().execute(security_group)
        return response.Response(
            {"status": _("Rules update was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    def destroy(self, request, *args, **kwargs):
        security_group: models.SecurityGroup = self.get_object()
        # Snapshot rules *before* the executor cascades them so we can record
        # them in the aggregate event's removed_rules list.
        old_snapshot = audit.snapshot_security_group_rules(security_group)
        diff = compute_collection_diff(
            old_snapshot,
            [],
            identity_key=lambda r: r["_pk"],
            compare_fields=audit.SECURITY_GROUP_RULE_COMPARE_FIELDS,
            serialize=lambda r: {k: v for k, v in r.items() if k != "_pk"},
        )
        audit.emit_security_group_rules_changed(
            security_group, diff, trigger="user_action"
        )
        return super().destroy(request, *args, **kwargs)

    set_rules_validators = [core_validators.StateValidator(CoreStates.OK)]
    set_rules_serializer_class = (
        serializers.OpenStackSecurityGroupRuleListUpdateSerializer
    )


@extend_schema_view(
    list=extend_schema(
        summary="List server groups",
        description="Get a list of server groups.",
    ),
    retrieve=extend_schema(
        summary="Get server group details",
        description="Retrieve details of a specific server group.",
    ),
    destroy=extend_schema(
        summary="Delete server group",
        description="Delete a server group.",
    ),
)
class ServerGroupViewSet(structure_views.ResourceViewSet):
    disabled_actions = ["update", "partial_update"]
    queryset = models.ServerGroup.objects.all().order_by("tenant__name")
    serializer_class = serializers.OpenStackServerGroupSerializer
    filterset_class = filters.ServerGroupFilter
    pull_executor = executors.ServerGroupPullExecutor
    delete_executor = executors.ServerGroupDeleteExecutor


@extend_schema_view(
    list=extend_schema(
        summary="List floating IPs",
        description="Get a list of floating IP addresses. Status *DOWN* means that floating IP is not linked to a VM, status *ACTIVE* means that it is in use.",
    ),
    retrieve=extend_schema(
        summary="Get floating IP details",
        description="Retrieve details of a specific floating IP address.",
    ),
    destroy=extend_schema(
        summary="Delete floating IP",
        description="Delete a floating IP address.",
    ),
)
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
        summary="Attach floating IP to a port",
        description="Attach floating IP to port",
        request=serializers.OpenStackFloatingIPAttachSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Detach floating IP from port",
        description="Detach floating IP from port",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Update floating IP description",
        description="Update description of the floating IP",
        request=serializers.OpenStackFloatingIPDescriptionUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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


@extend_schema_view(
    list=extend_schema(
        summary="List tenants",
        description="Get a list of OpenStack tenants.",
    ),
    retrieve=extend_schema(
        summary="Get tenant details",
        description="Retrieve details of a specific OpenStack tenant.",
    ),
    update=extend_schema(
        summary="Update tenant",
        description="Update an existing OpenStack tenant.",
    ),
    partial_update=extend_schema(
        summary="Partially update tenant",
        description="Update specific fields of an OpenStack tenant.",
    ),
)
class TenantViewSet(
    structure_views.ResourceViewSet, structure_views.AvailabilityCheckViewMixin
):
    queryset = models.Tenant.objects.all().order_by("name")
    serializer_class = serializers.OpenStackTenantSerializer
    filterset_class = structure_filters.BaseResourceFilter

    update_executor = executors.TenantUpdateExecutor
    pull_executor = executors.TenantPullExecutor
    disabled_actions = ["create", "destroy"]

    @extend_schema(
        summary="Set tenant quotas",
        description="""A quota can be set for a particular tenant. Only staff users and service provider owners/managers can do that.
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
- floating_ip_count - maximal number of floating IPs. Use 0 to deny, -1 for unlimited.
- network_count - maximal number of networks. Use 0 to deny, -1 for unlimited.
- subnet_count - maximal number of subnets. Use 0 to deny, -1 for unlimited.
- port_count - maximal number of ports. Use 0 to deny, -1 for unlimited.
- gigabytes_<volume_type_name> - maximal storage for a specific Cinder volume type, in GB.
  For example, gigabytes_ssd or gigabytes___DEFAULT__. Use -1 for unlimited.

It is possible to update quotas by one or by submitting all the fields in one request.
Waldur will attempt to update the provided quotas. Please note, that if provided quotas are
conflicting with the backend (e.g. requested number of instances is below of the already existing ones),
some quotas might not be applied.

.. _MiB: http://en.wikipedia.org/wiki/Mebibyte

Response code of a successful request is **202 ACCEPTED**.
In case tenant is in a non-stable status, the response would be **409 CONFLICT**.
In this case REST client is advised to repeat the request after some time.
On successful completion the task will synchronize quotas with the backend.
""",
        # Named fields give SDK consumers typed hints; additionalProperties covers
        # the dynamic gigabytes_<volume_type_name> keys (GB, min -1).
        # Using a raw media-type dict is the drf-spectacular 0.28 way to combine
        # both named properties and additionalProperties in one request schema.
        request={
            "application/json": {
                "type": "object",
                "additionalProperties": {
                    "type": "integer",
                    "minimum": -1,
                    "description": "Per-volume-type storage quota in GB (gigabytes_<type>). Use -1 for unlimited, 0 to deny.",
                },
                "properties": {
                    "instances": {"type": "integer", "minimum": 1},
                    "volumes": {"type": "integer", "minimum": 1},
                    "snapshots": {"type": "integer", "minimum": 1},
                    "ram": {"type": "integer", "minimum": 1, "description": "In MiB"},
                    "vcpu": {"type": "integer", "minimum": 1},
                    "storage": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "In MiB",
                    },
                    "security_group_count": {"type": "integer", "minimum": 1},
                    "security_group_rule_count": {"type": "integer", "minimum": 1},
                    "floating_ip_count": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Use 0 to deny, -1 for unlimited",
                    },
                    "network_count": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Use 0 to deny, -1 for unlimited",
                    },
                    "subnet_count": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Use 0 to deny, -1 for unlimited",
                    },
                    "port_count": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Use 0 to deny, -1 for unlimited",
                    },
                },
            }
        },
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
                    "floating_ip_count": 50,
                    "network_count": 10,
                    "subnet_count": 20,
                    "port_count": 100,
                    "gigabytes_ssd": 500,
                    "gigabytes___DEFAULT__": 1000,
                },
            )
        ],
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @extend_schema(responses={status.HTTP_200_OK: StatusSerializer})
    @decorators.action(detail=True, methods=["post"])
    def set_quotas(self, request, uuid=None):
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

    set_quotas_permissions = [
        openstack_permissions.can_update_tenant_quotas_as_service_provider
    ]
    set_quotas_validators = [core_validators.StateValidator(CoreStates.OK)]
    set_quotas_serializer_class = serializers.OpenStackTenantQuotaSerializer

    @extend_schema(
        summary="Create network for tenant",
        description="Create network for tenant",
        responses={status.HTTP_201_CREATED: serializers.OpenStackNetworkSerializer},
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
        summary="Create floating IP for tenant",
        description="Create floating IP for tenant",
        request=serializers.OpenStackFloatingIPSerializer,
        responses=serializers.OpenStackFloatingIPSerializer,
    )
    @decorators.action(detail=True, methods=["post"])
    def create_floating_ip(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        floating_ip = serializer.save()

        router = serializer.validated_data.get("router")
        executor_kwargs = {}
        if router:
            executor_kwargs["router"] = router

        executors.FloatingIPCreateExecutor.execute(floating_ip, **executor_kwargs)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_floating_ip_validators = [
        core_validators.StateValidator(CoreStates.OK),
        external_network_is_defined,
    ]
    create_floating_ip_serializer_class = serializers.OpenStackFloatingIPSerializer

    @extend_schema(
        summary="Pull floating IPs",
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

    @extend_schema(
        summary="Create security group",
        description="Create a security group for the tenant.",
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

        # Emit one aggregate audit event with all initial rules, instead of
        # leaving the backend layer to fan out N per-rule events.
        new_snapshot = audit.snapshot_security_group_rules(security_group)
        diff = compute_collection_diff(
            [],
            new_snapshot,
            identity_key=lambda r: r["_pk"],
            compare_fields=audit.SECURITY_GROUP_RULE_COMPARE_FIELDS,
            serialize=lambda r: {k: v for k, v in r.items() if k != "_pk"},
        )
        audit.emit_security_group_rules_changed(
            security_group, diff, trigger="user_action"
        )

        executors.SecurityGroupCreateExecutor().execute(security_group)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_security_group_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_security_group_serializer_class = (
        serializers.OpenStackSecurityGroupSerializer
    )

    @extend_schema(
        summary="Batch update security groups for a tenant.",
        description="""
        * Security groups with UUIDs are updated.
        * Security groups without UUIDs are created.
        * Security groups existing in the tenant but not present in the request are deleted.
        * Rules for created/updated security groups are replaced.

        To reference a remote group within a rule, use 'remote_group_name' field.""",
        request=serializers.TenantPushSecurityGroupsSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def push_security_groups(self, request, uuid=None):
        tenant: models.Tenant = self.get_object()
        serializer = self.get_serializer(instance=tenant, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.TenantPushSecurityGroupsExecutor.execute(tenant)

        return response.Response(
            {"status": _("Security groups update has been scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    push_security_groups_serializer_class = (
        serializers.TenantPushSecurityGroupsSerializer
    )
    push_security_groups_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        summary="Pull security groups",
        description="Trigger job to pull security groups from remote VPC",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Pull server groups",
        description="Trigger job to pull server groups from remote VPC",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Create server group",
        description="Create a new server group for the tenant.",
        examples=[
            OpenApiExample(
                request_only=True,
                name="openstack-tenant-create-server-group",
                value={"name": "Server group name", "policy": "affinity"},
            )
        ],
        responses={status.HTTP_201_CREATED: serializers.OpenStackServerGroupSerializer},
    )
    @extend_schema(
        responses={status.HTTP_201_CREATED: serializers.OpenStackServerGroupSerializer}
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
        summary="Change tenant user password",
        description="Change password for tenant user",
        request=serializers.OpenStackTenantChangePasswordSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Pull tenant quotas",
        description="It triggers celery job to pull quotas from remote VPC",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="List backend instances",
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
        summary="List backend volumes",
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

    @extend_schema(
        summary="Tenant network topology",
        description=(
            "Compose the tenant's network topology — routers, networks, subnets, "
            "ports, instances, floating IPs, external networks, and inbound RBAC "
            "shares — as a graph (nodes + edges). Read-only; all data comes from "
            "already-pulled state, no Neutron calls."
        ),
        request=None,
        responses={status.HTTP_200_OK: serializers.TenantTopologySerializer},
    )
    @decorators.action(detail=True, methods=["get"])
    def topology(self, request, uuid=None):
        tenant: models.Tenant = self.get_object()
        graph = topology.build_tenant_topology(tenant)
        return response.Response(graph, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(
        summary="List routers",
        description="Get a list of routers.",
    ),
    retrieve=extend_schema(
        summary="Get router details",
        description="Retrieve details of a specific router.",
    ),
    create=extend_schema(
        summary="Create router",
        description="Create a new router.",
    ),
    destroy=extend_schema(
        summary="Delete router",
        description="Delete a router.",
    ),
)
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

    @extend_schema(
        summary="Set static routes",
        description="Define or overwrite the static routes for the router.",
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
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
        summary="Add router interface",
        description="Add interface to router. Either subnet or port must be provided.",
        request=serializers.OpenStackRouterInterfaceSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Remove router interface",
        description="Remove interface from router. Either subnet or port must be provided.",
        request=serializers.OpenStackRouterInterfaceSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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

    @extend_schema(
        summary="Set external gateway",
        description=(
            "Set an external network as the gateway for this router. "
            "Advanced options (SNAT control, fixed IPs) require additional permissions."
        ),
        request=serializers.SetExternalGatewaySerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_external_gateway(self, request, uuid=None):
        router: models.Router = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        old_external_network_id = router.external_network_id
        router.external_network_id = data["external_network_id"]
        router.external_network_ref = data.get("external_network_ref")
        router.enable_snat = data.get("enable_snat")
        router.external_fixed_ips = data.get("external_fixed_ips", [])
        router.save(
            update_fields=[
                "external_network_id",
                "external_network_ref",
                "enable_snat",
                "external_fixed_ips",
            ]
        )
        executors.RouterSetExternalGatewayExecutor.execute(router)

        event_logger.emit(
            "External gateway has been set on router.",
            event_type=EventType.OPENSTACK_ROUTER_UPDATED,
            event_context={
                "router": router,
                "tenant_backend_id": router.tenant.backend_id,
                "old_external_network_id": old_external_network_id,
                "new_external_network_id": router.external_network_id,
                "enable_snat": router.enable_snat,
                "external_fixed_ips": router.external_fixed_ips,
            },
            scopes=[router, router.project, router.project.customer],
        )

        logger.info(
            "External gateway has been set on router %s to network %s "
            "(enable_snat=%s, external_fixed_ips=%s).",
            router,
            router.external_network_id,
            router.enable_snat,
            router.external_fixed_ips,
        )

        return response.Response(
            {"status": _("External gateway update was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    set_external_gateway_permissions = [
        openstack_permissions.can_manage_openstack_router_gateway
    ]
    set_external_gateway_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED)
    ]
    set_external_gateway_serializer_class = serializers.SetExternalGatewaySerializer

    @extend_schema(
        summary="Remove external gateway",
        description="Remove the external gateway from this router.",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def remove_external_gateway(self, request, uuid=None):
        router: models.Router = self.get_object()
        if not router.has_external_gateway:
            return response.Response(
                {"detail": _("Router does not have an external gateway.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Check for floating IPs associated via the gateway network
        floating_ip_count = models.FloatingIP.objects.filter(
            tenant=router.tenant,
            backend_network_id=router.external_network_id,
        ).count()
        if floating_ip_count > 0:
            return response.Response(
                {
                    "detail": _(
                        "Cannot remove external gateway: %d floating IP(s) "
                        "are still associated with this gateway network."
                    )
                    % floating_ip_count
                },
                status=status.HTTP_409_CONFLICT,
            )
        old_external_network_id = router.external_network_id
        executors.RouterRemoveExternalGatewayExecutor.execute(router)

        event_logger.emit(
            "External gateway has been removed from router.",
            event_type=EventType.OPENSTACK_ROUTER_UPDATED,
            event_context={
                "router": router,
                "tenant_backend_id": router.tenant.backend_id,
                "old_external_network_id": old_external_network_id,
                "new_external_network_id": "",
            },
            scopes=[router, router.project, router.project.customer],
        )

        logger.info(
            "External gateway (network %s) removal has been scheduled for router %s.",
            old_external_network_id,
            router,
        )

        return response.Response(
            {"status": _("External gateway removal was successfully scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )

    remove_external_gateway_permissions = [
        openstack_permissions.can_manage_openstack_router_gateway
    ]
    remove_external_gateway_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED)
    ]

    @extend_schema(
        summary="Effective routes for this router",
        description=(
            "Compose the router's routing table from three sources: the "
            "default route inherited from the external gateway subnet, the "
            "on-link routes implied by each attached interface, and the "
            "user-set static routes. SNAT state is reported alongside."
        ),
        request=None,
        responses={
            status.HTTP_200_OK: serializers.EffectiveRoutesResponseSerializer,
        },
    )
    @decorators.action(detail=True, methods=["get"])
    def effective_routes(self, request, uuid=None):
        router: models.Router = self.get_object()
        return response.Response(
            routes.compute_effective_routes(router),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="List available external networks",
        description=(
            "Returns a merged list of external networks available for this router's tenant, "
            "from both global external networks and RBAC-exposed networks."
        ),
        responses={200: serializers.AvailableExternalNetworkSerializer(many=True)},
    )
    @decorators.action(detail=True, methods=["get"])
    def available_external_networks(self, request, uuid=None):
        router: models.Router = self.get_object()
        tenant = router.tenant
        result = []

        # Global external networks
        for ext_net in models.ExternalNetwork.objects.filter(
            settings=tenant.service_settings
        ):
            subnets = [
                {
                    "backend_id": s.backend_id,
                    "name": s.name,
                    "cidr": getattr(s, "cidr", ""),
                }
                for s in ext_net.subnets.all()
            ]
            result.append(
                {
                    "backend_id": ext_net.backend_id,
                    "name": ext_net.name,
                    "description": ext_net.description,
                    "source": "global",
                    "subnets": subnets,
                }
            )

        # RBAC-exposed-as-external networks
        seen_backend_ids = {r["backend_id"] for r in result}
        rbac_networks = models.Network.objects.filter(
            rbac_policies__target_tenant=tenant,
            rbac_policies__policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
        ).distinct()
        for network in rbac_networks:
            if network.backend_id in seen_backend_ids:
                continue
            subnets = [
                {
                    "backend_id": s.backend_id,
                    "name": s.name,
                    "cidr": s.cidr,
                }
                for s in network.subnets.all()
            ]
            result.append(
                {
                    "backend_id": network.backend_id,
                    "name": network.name,
                    "description": network.description,
                    "source": "rbac",
                    "subnets": subnets,
                }
            )

        serializer = serializers.AvailableExternalNetworkSerializer(result, many=True)
        return response.Response(serializer.data)

    set_erred_serializer_class = structure_serializers.SetErredSerializer

    @extend_schema(
        summary="Mark router as ERRED",
        description=(
            "Manually transition the router to ERRED state. "
            "This is useful for routers stuck in transitional states "
            "(CREATING, UPDATING, DELETING) that cannot be synced via pull. "
            "Staff-only operation."
        ),
        responses={status.HTTP_200_OK: DetailSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_erred(self, request, uuid=None):
        resource = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource.error_message = serializer.validated_data.get("error_message", "")
        resource.error_traceback = serializer.validated_data.get("error_traceback", "")
        resource.set_erred()
        resource.save(update_fields=["state", "error_message", "error_traceback"])
        return response.Response(
            {"detail": _("Resource has been marked as ERRED.")},
            status=status.HTTP_200_OK,
        )

    set_erred_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Mark router as OK",
        description=(
            "Manually transition the router to OK state and clear error fields. "
            "Staff-only operation."
        ),
        request=None,
        responses={status.HTTP_200_OK: DetailSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_ok(self, request, uuid=None):
        resource = self.get_object()
        resource.error_message = ""
        resource.error_traceback = ""
        resource.set_ok()
        resource.save(update_fields=["state", "error_message", "error_traceback"])
        return response.Response(
            {"detail": _("Resource has been marked as OK.")},
            status=status.HTTP_200_OK,
        )

    set_ok_permissions = [structure_permissions.is_staff]


@extend_schema_view(
    list=extend_schema(
        summary="List load balancers",
        description="Get a list of load balancers.",
    ),
    retrieve=extend_schema(
        summary="Get load balancer details",
        description="Retrieve details of a specific load balancer.",
    ),
    create=extend_schema(
        summary="Create load balancer",
        description="Create a new load balancer.",
    ),
    update=extend_schema(
        summary="Update load balancer",
        description="Update an existing load balancer.",
    ),
    partial_update=extend_schema(
        summary="Partially update load balancer",
        description="Update specific fields of a load balancer.",
    ),
    destroy=extend_schema(
        summary="Delete load balancer",
        description="Delete a load balancer.",
    ),
)
class LoadBalancerViewSet(
    LBaaSAuditMixin, core_mixins.ExecutorMixin, core_views.ActionsViewSet
):
    lookup_field = "uuid"
    queryset = (
        models.LoadBalancer.objects.all()
        .order_by("tenant__name")
        .select_related("vip_subnet", "vip_port", "attached_floating_ip")
        .prefetch_related("vip_port__security_groups")
    )
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.LoadBalancerFilter
    serializer_class = serializers.OpenStackLoadBalancerSerializer
    create_serializer_class = serializers.CreateLoadBalancerSerializer
    update_serializer_class = serializers.UpdateLoadBalancerSerializer
    partial_update_serializer_class = serializers.UpdateLoadBalancerSerializer

    delete_executor = executors.LoadBalancerDeleteExecutor
    create_executor = executors.LoadBalancerCreateExecutor
    update_executor = executors.LoadBalancerUpdateExecutor

    @extend_schema(
        summary="Unlink load balancer",
        description=(
            "Delete the load balancer from the Waldur database without scheduling "
            "operations on the OpenStack backend and without checking resource state. "
            "Staff-only; intended for cleaning up records stuck in transitional states."
        ),
        request=None,
        responses={204: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def unlink(self, request, uuid=None):
        load_balancer = self.get_object()
        load_balancer.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)

    unlink_permissions = [structure_permissions.is_staff]

    @extend_schema(
        summary="Attach floating IP to VIP",
        description="Attach a floating IP to the load balancer VIP port.",
        request=serializers.LoadBalancerAttachFloatingIPSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def attach_floating_ip(self, request, uuid=None):
        load_balancer: models.LoadBalancer = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        floating_ip: models.FloatingIP = serializer.validated_data["floating_ip"]
        if load_balancer.state != CoreStates.OK:
            raise core_exceptions.IncorrectStateException(
                _("Load balancer [%(lb)s] must be in OK state, current: [%(state)s]")
                % {
                    "lb": load_balancer,
                    "state": load_balancer.get_state_display(),
                }
            )
        if not load_balancer.vip_port or not load_balancer.vip_port.backend_id:
            raise exceptions.ValidationError(
                _(
                    "Load balancer VIP port is not available yet. "
                    "Wait for the load balancer to become ACTIVE."
                )
            )
        if floating_ip.tenant != load_balancer.tenant:
            raise exceptions.ValidationError(
                _("Floating IP must belong to the same tenant as the load balancer.")
            )
        executors.LoadBalancerAttachFloatingIPExecutor().execute(
            load_balancer,
            floating_ip=core_utils.serialize_instance(floating_ip),
        )
        return response.Response(
            {"status": _("Attach was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    attach_floating_ip_serializer_class = (
        serializers.LoadBalancerAttachFloatingIPSerializer
    )
    attach_floating_ip_validators = [
        core_validators.StateValidator(CoreStates.OK),
    ]

    @extend_schema(
        summary="Detach floating IP from VIP",
        description="Detach floating IP from the load balancer VIP port.",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def detach_floating_ip(self, request, uuid=None):
        load_balancer: models.LoadBalancer = self.get_object()
        if not load_balancer.attached_floating_ip:
            raise exceptions.ValidationError(
                _("Load balancer has no floating IP attached.")
            )
        executors.LoadBalancerDetachFloatingIPExecutor().execute(load_balancer)
        return response.Response(
            {"status": _("Detach was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    detach_floating_ip_validators = [
        core_validators.StateValidator(CoreStates.OK),
    ]

    @extend_schema(
        summary="Set security groups on VIP port",
        description="Set security groups on the load balancer VIP port to control access.",
        request=serializers.LoadBalancerSetSecurityGroupsSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def set_security_groups(self, request, uuid=None):
        load_balancer = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        security_groups = serializer.validated_data["security_groups"]
        if not load_balancer.vip_port or not load_balancer.vip_port.backend_id:
            raise exceptions.ValidationError(
                _(
                    "Load balancer VIP port is not available yet. "
                    "Wait for the load balancer to become ACTIVE."
                )
            )
        for sg in security_groups:
            if sg.tenant != load_balancer.tenant:
                raise exceptions.ValidationError(
                    _(
                        "Security group '%(sg)s' must belong to the same tenant "
                        "as the load balancer."
                    )
                    % {"sg": sg.name}
                )
        old_sgs = list(load_balancer.vip_port.security_groups.all())
        audit.emit_load_balancer_security_groups_changed(
            load_balancer, old_sgs=old_sgs, new_sgs=security_groups
        )
        executors.LoadBalancerSetSecurityGroupsExecutor().execute(
            load_balancer,
            security_groups=[
                core_utils.serialize_instance(sg) for sg in security_groups
            ],
        )
        return response.Response(
            {"status": _("Setting security groups was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    set_security_groups_serializer_class = (
        serializers.LoadBalancerSetSecurityGroupsSerializer
    )
    set_security_groups_validators = [
        core_validators.StateValidator(CoreStates.OK),
    ]

    @extend_schema(
        summary="Pull load balancer",
        description="Synchronize load balancer state from the OpenStack backend.",
        request=None,
        responses={202: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        load_balancer = self.get_object()
        executors.LoadBalancerPullExecutor.execute(load_balancer)
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED),
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List pools",
        description="Get a list of load balancer pools.",
    ),
    retrieve=extend_schema(
        summary="Get pool details",
        description="Retrieve details of a specific pool.",
    ),
    create=extend_schema(
        summary="Create pool",
        description="Create a new pool for a load balancer.",
    ),
    update=extend_schema(
        summary="Update pool",
        description="Update an existing pool.",
    ),
    partial_update=extend_schema(
        summary="Partially update pool",
        description="Update specific fields of a pool.",
    ),
    destroy=extend_schema(
        summary="Delete pool",
        description="Delete a pool.",
    ),
)
class PoolViewSet(
    LBaaSAuditMixin, core_mixins.ExecutorMixin, core_views.ActionsViewSet
):
    lookup_field = "uuid"
    queryset = models.Pool.objects.all().order_by("load_balancer__name", "name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.PoolFilter
    serializer_class = serializers.OpenStackPoolSerializer
    create_serializer_class = serializers.CreatePoolSerializer
    update_serializer_class = serializers.UpdatePoolSerializer
    partial_update_serializer_class = serializers.UpdatePoolSerializer

    delete_executor = executors.PoolDeleteExecutor
    create_executor = executors.PoolCreateExecutor
    update_executor = executors.PoolUpdateExecutor

    @extend_schema(
        summary="Pull pool",
        description="Synchronize pool state from the OpenStack backend. Also pulls the associated load balancer.",
        request=None,
        responses={202: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        pool = self.get_object()
        executors.PoolPullExecutor.execute(
            pool,
            serialized_load_balancer=core_utils.serialize_instance(pool.load_balancer),
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED),
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List listeners",
        description="Get a list of load balancer listeners.",
    ),
    retrieve=extend_schema(
        summary="Get listener details",
        description="Retrieve details of a specific listener.",
    ),
    create=extend_schema(
        summary="Create listener",
        description="Create a new listener for a load balancer.",
    ),
    update=extend_schema(
        summary="Update listener",
        description="Update an existing listener.",
    ),
    partial_update=extend_schema(
        summary="Partially update listener",
        description="Update specific fields of a listener.",
    ),
    destroy=extend_schema(
        summary="Delete listener",
        description="Delete a listener.",
    ),
)
class ListenerViewSet(
    LBaaSAuditMixin, core_mixins.ExecutorMixin, core_views.ActionsViewSet
):
    lookup_field = "uuid"
    queryset = models.Listener.objects.all().order_by(
        "load_balancer__name", "protocol_port", "name"
    )
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.ListenerFilter
    serializer_class = serializers.OpenStackListenerSerializer
    create_serializer_class = serializers.CreateListenerSerializer
    update_serializer_class = serializers.UpdateListenerSerializer
    partial_update_serializer_class = serializers.UpdateListenerSerializer

    delete_executor = executors.ListenerDeleteExecutor
    create_executor = executors.ListenerCreateExecutor
    update_executor = executors.ListenerUpdateExecutor

    @extend_schema(
        summary="Pull listener",
        description="Synchronize listener state from the OpenStack backend. Also pulls pools of the load balancer and the load balancer itself.",
        request=None,
        responses={202: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        listener = self.get_object()
        executors.ListenerPullExecutor.execute(
            listener,
            serialized_load_balancer=core_utils.serialize_instance(
                listener.load_balancer
            ),
            serialized_tenant=core_utils.serialize_instance(
                listener.load_balancer.tenant
            ),
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED),
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List pool members",
        description="Get a list of pool members.",
    ),
    retrieve=extend_schema(
        summary="Get pool member details",
        description="Retrieve details of a specific pool member.",
    ),
    create=extend_schema(
        summary="Create pool member",
        description="Create a new member for a pool.",
    ),
    update=extend_schema(
        summary="Update pool member",
        description="Update an existing pool member.",
    ),
    partial_update=extend_schema(
        summary="Partially update pool member",
        description="Update specific fields of a pool member.",
    ),
    destroy=extend_schema(
        summary="Delete pool member",
        description="Delete a pool member.",
    ),
)
class PoolMemberViewSet(
    LBaaSAuditMixin, core_mixins.ExecutorMixin, core_views.ActionsViewSet
):
    lookup_field = "uuid"
    queryset = models.PoolMember.objects.all().order_by(
        "pool__load_balancer__name", "pool__name", "address", "protocol_port"
    )
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.PoolMemberFilter
    serializer_class = serializers.OpenStackPoolMemberSerializer
    create_serializer_class = serializers.CreatePoolMemberSerializer
    update_serializer_class = serializers.UpdatePoolMemberSerializer
    partial_update_serializer_class = serializers.UpdatePoolMemberSerializer

    delete_executor = executors.PoolMemberDeleteExecutor
    create_executor = executors.PoolMemberCreateExecutor
    update_executor = executors.PoolMemberUpdateExecutor

    @extend_schema(
        summary="Pull pool member",
        description="Synchronize pool member state from the OpenStack backend. Also pulls the associated pool and load balancer.",
        request=None,
        responses={202: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        member = self.get_object()
        executors.PoolMemberPullExecutor.execute(
            member,
            serialized_pool=core_utils.serialize_instance(member.pool),
            serialized_load_balancer=core_utils.serialize_instance(
                member.pool.load_balancer
            ),
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED),
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List health monitors",
        description="Get a list of pool health monitors.",
    ),
    retrieve=extend_schema(
        summary="Get health monitor details",
        description="Retrieve details of a specific health monitor.",
    ),
    create=extend_schema(
        summary="Create health monitor",
        description="Create a new health monitor for a pool.",
    ),
    update=extend_schema(
        summary="Update health monitor",
        description="Update an existing health monitor.",
    ),
    partial_update=extend_schema(
        summary="Partially update health monitor",
        description="Update specific fields of a health monitor.",
    ),
    destroy=extend_schema(
        summary="Delete health monitor",
        description="Delete a health monitor.",
    ),
)
class HealthMonitorViewSet(core_mixins.ExecutorMixin, core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.HealthMonitor.objects.all().order_by(
        "pool__load_balancer__name", "pool__name", "name"
    )
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.HealthMonitorFilter
    serializer_class = serializers.OpenStackHealthMonitorSerializer
    create_serializer_class = serializers.CreateHealthMonitorSerializer
    update_serializer_class = serializers.UpdateHealthMonitorSerializer
    partial_update_serializer_class = serializers.UpdateHealthMonitorSerializer

    delete_executor = executors.HealthMonitorDeleteExecutor
    create_executor = executors.HealthMonitorCreateExecutor
    update_executor = executors.HealthMonitorUpdateExecutor

    @extend_schema(
        summary="Pull health monitor",
        description="Synchronize health monitor state from the OpenStack backend. Also pulls the associated pool and load balancer.",
        request=None,
        responses={202: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def pull(self, request, uuid=None):
        hm = self.get_object()
        executors.HealthMonitorPullExecutor.execute(
            hm,
            serialized_pool=core_utils.serialize_instance(hm.pool),
            serialized_load_balancer=core_utils.serialize_instance(
                hm.pool.load_balancer
            ),
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    pull_validators = [
        core_validators.StateValidator(CoreStates.OK, CoreStates.ERRED),
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List ports",
        description="Get a list of network ports.",
    ),
    retrieve=extend_schema(
        summary="Get port details",
        description="Retrieve details of a specific network port.",
    ),
    create=extend_schema(
        summary="Create port",
        description="Create a new network port.",
    ),
    update=extend_schema(
        summary="Update port",
        description="Update an existing network port.",
    ),
    partial_update=extend_schema(
        summary="Partially update port",
        description="Update specific fields of a network port.",
    ),
    destroy=extend_schema(
        summary="Delete port",
        description="Delete a network port.",
    ),
)
class PortViewSet(structure_views.ResourceViewSet):
    queryset = models.Port.objects.all().order_by("network__name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.PortFilter
    serializer_class = serializers.OpenStackPortSerializer

    create_executor = executors.PortCreateExecutor
    update_executor = executors.PortUpdateNameAndDescriptionExecutor
    delete_executor = executors.PortDeleteExecutor

    @extend_schema(
        summary="Enable port security",
        description="Enable port security for the port",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def enable_port_security(self, request, uuid=None):
        port = self.get_object()
        backend = port.get_backend()
        backend.enable_port_security(port)

        was_enabled = port.port_security_enabled
        port.port_security_enabled = True
        port.save(update_fields=["port_security_enabled"])

        if not was_enabled:
            audit.emit_port_security_toggled(port, enabled=True)

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Disable port security",
        description="Disable port security for the port",
        request=None,
        responses={status.HTTP_200_OK: None},
    )
    @decorators.action(detail=True, methods=["post"])
    def disable_port_security(self, request, uuid=None):
        port = self.get_object()
        backend = port.get_backend()
        backend.disable_port_security(port)

        was_enabled = port.port_security_enabled
        port.port_security_enabled = False
        port.security_groups.clear()  # Remove all security groups
        port.save(update_fields=["port_security_enabled"])

        if was_enabled:
            audit.emit_port_security_toggled(port, enabled=False)

        return response.Response(status=status.HTTP_200_OK)

    @extend_schema(
        summary="Enable port",
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
        summary="Disable port",
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
        summary="Update port IP address",
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
        summary="Update port security groups",
        description="Update security groups of the port",
        request=serializers.OpenStackInstanceSecurityGroupsUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_security_groups(self, request, uuid=None):
        port: models.Port = self.get_object()
        serializer = self.get_serializer(port, data=request.data)
        serializer.is_valid(raise_exception=True)
        old_sgs = list(port.security_groups.all())
        serializer.save()
        audit.emit_port_security_groups_changed(
            port, old_sgs=old_sgs, new_sgs=list(port.security_groups.all())
        )

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

    @extend_schema(
        summary="Set allowed address pairs",
        description=(
            "Replace the Port's allowed_address_pairs list. Cluster-VIP "
            "workloads (keepalived, MetalLB, OpenShift ingress, OVN router) "
            "need ports to permit additional IP/MAC pairs beyond their "
            "fixed IPs. Values are validated and pushed to Neutron."
        ),
        request=serializers.SetAllowedAddressPairsSerializer,
        responses={
            status.HTTP_200_OK: serializers.OpenStackPortSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def set_allowed_address_pairs(self, request, uuid=None):
        port: models.Port = self.get_object()
        serializer = serializers.SetAllowedAddressPairsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_pairs = list(serializer.validated_data["allowed_address_pairs"])
        old_pairs = list(port.allowed_address_pairs or [])

        backend = port.get_backend()
        backend.set_port_allowed_address_pairs(port, new_pairs)
        port.allowed_address_pairs = new_pairs
        port.save(update_fields=["allowed_address_pairs"])

        audit.emit_allowed_address_pairs_changed(
            port, old_pairs=old_pairs, new_pairs=new_pairs
        )

        result = self.get_serializer(port, context={"request": request})
        return response.Response(result.data, status=status.HTTP_200_OK)

    set_allowed_address_pairs_serializer_class = (
        serializers.SetAllowedAddressPairsSerializer
    )
    set_allowed_address_pairs_validators = [
        core_validators.StateValidator(CoreStates.OK),
    ]
    # AAP is a layer-2/3 spoofing primitive — must match the gate used by
    # the existing instance-level ``update_allowed_address_pairs`` action,
    # not the default ``is_administrator`` that ``ResourceViewSet`` would
    # otherwise apply.
    set_allowed_address_pairs_permissions = [
        openstack_permissions.can_manage_openstack_instance
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List networks",
        description="Get a list of networks.",
    ),
    retrieve=extend_schema(
        summary="Get network details",
        description="Retrieve details of a specific network.",
    ),
    update=extend_schema(
        summary="Update network",
        description="Update an existing network.",
    ),
    partial_update=extend_schema(
        summary="Partially update network",
        description="Update specific fields of a network.",
    ),
    destroy=extend_schema(
        summary="Delete network",
        description="Delete a network.",
    ),
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

    @extend_schema(
        summary="Create subnet",
        description="Create a new subnet within the network.",
        responses={status.HTTP_201_CREATED: serializers.OpenStackSubNetSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def create_subnet(self, request, uuid=None):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subnet = serializer.save()
        executors.SubNetCreateExecutor.execute(subnet)
        return response.Response(serializer.data, status=status.HTTP_201_CREATED)

    create_subnet_validators = [core_validators.StateValidator(CoreStates.OK)]
    create_subnet_serializer_class = serializers.OpenStackSubNetSerializer

    @extend_schema(
        summary="Set network MTU",
        description="Update the Maximum Transmission Unit (MTU) for the network.",
        responses={status.HTTP_202_ACCEPTED: serializers.SetMtuSerializer},
    )
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
        deprecated=True,
        summary="Create RBAC policy",
        description="Create RBAC policy for the network. DEPRECATED: please use the dedicated /api/openstack-network-rbac-policies/ endpoint.",
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
        deprecated=True,
        summary="Delete RBAC policy",
        description="Delete RBAC policy for the network. DEPRECATED: please use the dedicated /api/openstack-network-rbac-policies/ endpoint.",
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


@extend_schema_view(
    list=extend_schema(
        summary="List subnets",
        description="Get a list of subnets.",
    ),
    retrieve=extend_schema(
        summary="Get subnet details",
        description="Retrieve details of a specific subnet.",
    ),
    update=extend_schema(
        summary="Update subnet",
        description="Update an existing subnet.",
    ),
    partial_update=extend_schema(
        summary="Partially update subnet",
        description="Update specific fields of a subnet.",
    ),
    destroy=extend_schema(
        summary="Delete subnet",
        description="Delete a subnet.",
    ),
)
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

    @extend_schema(
        request=None,
        summary="Connect subnet to router",
        description="Connect the subnet to the default tenant router.",
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def connect(self, request, uuid=None):
        executors.SubnetConnectExecutor.execute(self.get_object())
        return response.Response(status=status.HTTP_202_ACCEPTED)

    connect_validators = [core_validators.StateValidator(CoreStates.OK)]

    @extend_schema(
        request=None,
        summary="Disconnect subnet from router",
        description="Disconnect the subnet from the default tenant router.",
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def disconnect(self, request, uuid=None):
        executors.SubnetDisconnectExecutor.execute(self.get_object())
        return response.Response(status=status.HTTP_202_ACCEPTED)

    disconnect_validators = [core_validators.StateValidator(CoreStates.OK)]


@extend_schema_view(
    list=extend_schema(
        summary="List volumes",
        description="Get a list of volumes.",
    ),
    retrieve=extend_schema(
        summary="Get volume details",
        description="Retrieve details of a specific volume.",
    ),
    update=extend_schema(
        summary="Update volume",
        description="Update an existing volume.",
    ),
    partial_update=extend_schema(
        summary="Partially update volume",
        description="Update specific fields of a volume.",
    ),
)
class VolumeViewSet(
    structure_views.ResourceViewSet, structure_views.AvailabilityCheckViewMixin
):
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
        summary="Extend volume size",
        description="Increase volume size",
        request=serializers.OpenStackVolumeExtendSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Create volume snapshot",
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
        summary="Attach volume to instance",
        description="Attach volume to instance",
        request=serializers.VolumeAttachSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Detach volume from instance",
        description="Detach instance from volume",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Change volume type",
        description="Retype detached volume",
        request=serializers.OpenStackVolumeRetypeSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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


@extend_schema_view(
    list=extend_schema(
        summary="List snapshots",
        description="Get a list of snapshots.",
    ),
    retrieve=extend_schema(
        summary="Get snapshot details",
        description="Retrieve details of a specific snapshot.",
    ),
    update=extend_schema(
        summary="Update snapshot",
        description="Update an existing snapshot.",
    ),
    partial_update=extend_schema(
        summary="Partially update snapshot",
        description="Update specific fields of a snapshot.",
    ),
    destroy=extend_schema(
        summary="Delete snapshot",
        description="Delete a snapshot.",
    ),
)
class SnapshotViewSet(structure_views.ResourceViewSet):
    queryset = models.Snapshot.objects.all().order_by("name")
    serializer_class = serializers.OpenStackSnapshotSerializer
    update_executor = executors.SnapshotUpdateExecutor
    delete_executor = executors.SnapshotDeleteExecutor
    pull_executor = executors.SnapshotPullExecutor
    filterset_class = filters.SnapshotFilter
    disabled_actions = ["create"]

    def destroy(self, request, *args, **kwargs):
        snapshot = self.get_object()
        for backup in snapshot.backups.all():
            backup.delete()
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        summary="Restore volume from snapshot",
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
        summary="List snapshot restorations",
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


@extend_schema_view(
    list=extend_schema(
        summary="List instance availability zones",
        description="Get a list of instance availability zones.",
    ),
    retrieve=extend_schema(
        summary="Get instance availability zone details",
        description="Retrieve details of a specific instance availability zone.",
    ),
)
class InstanceAvailabilityZoneViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.InstanceAvailabilityZone.objects.all().order_by(
        "settings", "name"
    )
    serializer_class = serializers.OpenStackInstanceAvailabilityZoneSerializer
    lookup_field = "uuid"
    filterset_class = filters.InstanceAvailabilityZoneFilter


@extend_schema_view(
    list=extend_schema(
        summary="List instances",
        description="Get a list of VM instances.",
    ),
    retrieve=extend_schema(
        summary="Get instance details",
        description="Retrieve details of a specific VM instance.",
    ),
    update=extend_schema(
        summary="Update instance",
        description="Update an existing VM instance.",
    ),
    partial_update=extend_schema(
        summary="Partially update instance",
        description="Update specific fields of a VM instance.",
    ),
)
class InstanceViewSet(
    structure_views.ResourceViewSet, structure_views.AvailabilityCheckViewMixin
):
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
        summary="Change instance flavor",
        description="Change flavor of the instance",
        request=serializers.InstanceFlavorChangeSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
        summary="Diagnose connectivity",
        description=(
            "Walks the wiring that connects this instance to the requested "
            "target (default 'external') and returns a per-check report "
            "computed from Waldur's already-pulled state — no live "
            "OpenStack call. Use to triage 'VM can't reach the internet' "
            "or 'VIP doesn't work' tickets in one click."
        ),
        request=serializers.DiagnoseConnectivityRequestSerializer,
        responses={
            status.HTTP_200_OK: serializers.DiagnoseConnectivityResponseSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def diagnose_connectivity(self, request, uuid=None):
        from waldur_openstack import diagnose

        instance: models.Instance = self.get_object()
        serializer = serializers.DiagnoseConnectivityRequestSerializer(
            data=request.data or {}
        )
        serializer.is_valid(raise_exception=True)
        target = serializer.validated_data.get("target") or "external"

        report = diagnose.run_diagnose(instance, target=target)
        payload = {
            "target": report.target,
            "target_address": report.target_address,
            "checks": [
                {
                    "check": c.check,
                    "status": c.status,
                    "detail": c.detail,
                    "fix_hint": c.fix_hint,
                }
                for c in report.checks
            ],
            "root_cause": report.root_cause,
        }
        response_serializer = serializers.DiagnoseConnectivityResponseSerializer(
            payload
        )
        return response.Response(response_serializer.data)

    diagnose_connectivity_serializer_class = (
        serializers.DiagnoseConnectivityRequestSerializer
    )
    change_flavor_permissions = [openstack_permissions.can_manage_openstack_instance]

    @extend_schema(
        summary="Start instance",
        description="Start the instance",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
    start_permissions = [openstack_permissions.can_manage_openstack_instance_power]

    @extend_schema(
        summary="Stop instance",
        description="Stop the instance",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
    stop_permissions = [openstack_permissions.can_manage_openstack_instance_power]

    @extend_schema(
        summary="Restart instance",
        description="Restart the instance",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
    restart_permissions = [openstack_permissions.can_manage_openstack_instance_power]

    @extend_schema(
        summary="Rescue instance",
        description=(
            "Boot the instance from a separate rescue image while keeping "
            "the original disk attached. Volume-backed instances require an "
            "explicit rescue_image with hw_rescue_device or hw_rescue_bus set."
        ),
        request=serializers.InstanceRescueSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def rescue(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        rescue_image = serializer.validated_data.get("rescue_image")
        executors.InstanceRescueExecutor().execute(
            instance,
            rescue_image_ref=rescue_image.backend_id if rescue_image else None,
        )
        return response.Response(
            {"status": _("rescue was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    rescue_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.ACTIVE),
    ]
    rescue_permissions = [openstack_permissions.can_manage_openstack_instance_power]
    rescue_serializer_class = serializers.InstanceRescueSerializer

    @extend_schema(
        summary="Unrescue instance",
        description="Restore the instance from rescue mode.",
        request=None,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def unrescue(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        executors.InstanceUnrescueExecutor().execute(instance)
        return response.Response(
            {"status": _("unrescue was scheduled")}, status=status.HTTP_202_ACCEPTED
        )

    unrescue_validators = [
        core_validators.StateValidator(CoreStates.OK),
        core_validators.RuntimeStateValidator(models.Instance.RuntimeStates.RESCUE),
    ]
    unrescue_permissions = [openstack_permissions.can_manage_openstack_instance_power]

    @extend_schema(
        summary="Update instance security groups",
        description="Update security groups of the instance",
        request=serializers.OpenStackInstanceSecurityGroupsUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_security_groups(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        old_sgs = list(instance.security_groups.all())
        serializer.save()
        audit.emit_instance_security_groups_changed(
            instance,
            old_sgs=old_sgs,
            new_sgs=list(instance.security_groups.all()),
        )

        executors.InstanceUpdateSecurityGroupsExecutor().execute(instance)
        return response.Response(
            {"status": _("security groups update was scheduled")},
            status=status.HTTP_202_ACCEPTED,
        )

    update_security_groups_validators = [core_validators.StateValidator(CoreStates.OK)]
    update_security_groups_permissions = [
        openstack_permissions.can_manage_openstack_instance
    ]
    update_security_groups_serializer_class = (
        serializers.OpenStackInstanceSecurityGroupsUpdateSerializer
    )

    @extend_schema(
        summary="Create instance backup",
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
    backup_permissions = [openstack_permissions.can_manage_openstack_instance]
    backup_serializer_class = serializers.OpenStackBackupSerializer

    @extend_schema(
        summary="Update instance allowed address pairs",
        description="Update allowed address pairs of the instance",
        request=serializers.OpenStackInstanceAllowedAddressPairsUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
    )
    @decorators.action(detail=True, methods=["post"])
    def update_allowed_address_pairs(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        subnet = serializer.validated_data["subnet"]
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

        old_pairs = list(port.allowed_address_pairs or [])
        serializer.save()
        allowed_address_pairs = serializer.validated_data["allowed_address_pairs"]
        audit.emit_allowed_address_pairs_changed(
            port, old_pairs=old_pairs, new_pairs=allowed_address_pairs
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
    update_allowed_address_pairs_permissions = [
        openstack_permissions.can_manage_openstack_instance
    ]
    update_allowed_address_pairs_serializer_class = (
        serializers.OpenStackInstanceAllowedAddressPairsUpdateSerializer
    )

    @extend_schema(
        summary="Update instance ports",
        description="Update ports of the instance",
        request=serializers.OpenStackInstancePortsUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
    update_ports_permissions = [openstack_permissions.can_manage_openstack_instance]
    update_ports_serializer_class = serializers.OpenStackInstancePortsUpdateSerializer

    @extend_schema(
        summary="List instance ports",
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
        summary="Update instance floating IPs",
        description="Update floating IPs of the instance",
        request=serializers.OpenStackInstanceFloatingIPsUpdateSerializer,
        responses={status.HTTP_202_ACCEPTED: StatusSerializer},
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
    update_floating_ips_permissions = [
        openstack_permissions.can_manage_openstack_instance
    ]
    update_floating_ips_serializer_class = (
        serializers.OpenStackInstanceFloatingIPsUpdateSerializer
    )

    @extend_schema(
        summary="List instance floating IPs",
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
        summary="Get console URL",
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

    console_permissions = [openstack_permissions.has_permissions_for_console]

    @extend_schema(
        summary="Get console log",
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
    console_log_permissions = [openstack_permissions.has_permissions_for_console]

    @extend_schema(
        summary="Get Placement allocations for the instance",
        description=(
            "Return what the OpenStack Placement service records as currently "
            "allocated to this instance, broken down by resource provider. "
            "Useful for diagnostics — especially for non-classic resources "
            "(VGPU, PCI_DEVICE, custom classes) that the flavor alone does "
            "not describe. Returns an empty list when Placement has no record "
            "(e.g. transient state right after create, or pre-Placement clouds)."
        ),
        request=None,
        responses={200: serializers.InstancePlacementAllocationSerializer(many=True)},
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def placement_allocations(self, request, uuid=None):
        instance: models.Instance = self.get_object()
        backend = instance.get_backend()
        try:
            data = backend.get_instance_placement_allocations(instance)
        except OpenStackBackendError as e:
            raise exceptions.ValidationError(str(e))
        serializer = serializers.InstancePlacementAllocationSerializer(data, many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    placement_allocations_validators = [core_validators.StateValidator(CoreStates.OK)]
    # Sysadmin-scope diagnostic — Placement RP UUIDs/names are fleet-topology
    # data, not end-user info. Restrict to staff, support and service-provider
    # owners (mirrors Hypervisor's `Permissions.customer_path = "settings__customer"`).
    placement_allocations_permissions = [
        openstack_permissions.can_diagnose_openstack_instance
    ]


@extend_schema_view(
    list=extend_schema(
        summary="List instances created via Marketplace",
        description="Get a list of VM instances that were created via the Marketplace.",
    ),
    retrieve=extend_schema(
        summary="Get instance created via Marketplace",
        description="Retrieve details of a specific VM instance created via the Marketplace.",
    ),
    create=extend_schema(
        summary="Create instance via Marketplace",
        description="Create a new VM instance through a Marketplace offering.",
    ),
    destroy=extend_schema(
        summary="Destroy instance created via Marketplace",
        description="Delete a VM instance created via the Marketplace.",
    ),
)
class MarketplaceInstanceViewSet(structure_views.ResourceViewSet):
    queryset = models.Instance.objects.all()
    serializer_class = serializers.OpenStackInstanceCreateSerializer
    filter_backends = structure_views.ResourceViewSet.filter_backends + (
        structure_filters.StartTimeFilter,
    )

    @extend_schema(
        summary="Force destroy instance",
        description="Forcefully destroy the instance, bypassing some state checks. This action is intended for recovery from failed states and should be used with caution.",
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


@extend_schema_view(
    list=extend_schema(
        summary="List volumes created via Marketplace",
        description="Get a list of volumes that were created via the Marketplace.",
    ),
    retrieve=extend_schema(
        summary="Get volume created via Marketplace",
        description="Retrieve details of a specific volume created via the Marketplace.",
    ),
    create=extend_schema(
        summary="Create volume via Marketplace",
        description="Create a new volume through a Marketplace offering.",
    ),
    destroy=extend_schema(
        summary="Destroy volume created via Marketplace",
        description="Delete a volume created via the Marketplace.",
    ),
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


@extend_schema_view(
    list=extend_schema(
        summary="List backups",
        description="Get a list of instance backups.",
    ),
    retrieve=extend_schema(
        summary="Get backup details",
        description="Retrieve details of a specific instance backup.",
    ),
    update=extend_schema(
        summary="Update backup",
        description="Update an existing instance backup.",
    ),
    partial_update=extend_schema(
        summary="Partially update backup",
        description="Update specific fields of an instance backup.",
    ),
    destroy=extend_schema(
        summary="Delete backup",
        description="Delete an instance backup.",
    ),
)
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
        summary="Restore instance from backup",
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


@extend_schema_view(
    list=extend_schema(
        summary="List volume availability zones",
        description="Get a list of volume availability zones.",
    ),
    retrieve=extend_schema(
        summary="Get volume availability zone details",
        description="Retrieve details of a specific volume availability zone.",
    ),
)
class VolumeAvailabilityZoneViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.VolumeAvailabilityZone.objects.all().order_by("settings", "name")
    serializer_class = serializers.OpenStackVolumeAvailabilityZoneSerializer
    lookup_field = "uuid"
    filterset_class = filters.VolumeAvailabilityZoneFilter


@extend_schema_view(
    list=extend_schema(
        summary="List network RBAC policies",
        description="Get a list of network RBAC policies.",
    ),
    retrieve=extend_schema(
        summary="Get network RBAC policy details",
        description="Retrieve details of a specific network RBAC policy.",
    ),
)
class NetworkRBACPolicyViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.NetworkRBACPolicy.objects.all().order_by("-created")
    serializer_class = serializers.NetworkRBACPolicySerializer
    # Visibility is handled explicitly in get_queryset (outbound + inbound),
    # so we don't layer GenericRoleFilter on top — it would re-apply the
    # source-side filter and drop inbound rows.
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.NetworkRBACPolicyFilter

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_support:
            return self.queryset
        # Build a single Q that covers both directions:
        # - outbound: user has admin/manager on the source network's project,
        #   or owns the source customer (mirrors the GenericRoleFilter logic
        #   that the legacy view applied via filter_queryset_for_user).
        # - inbound: user has admin/manager on the target tenant's project,
        #   or owns the target customer — they are the consumer of the share
        #   and need to inspect/audit it.
        from waldur_core.structure.managers import (
            get_connected_customers,
            get_connected_projects,
        )

        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)
        return self.queryset.filter(
            Q(network__tenant__project__in=connected_projects)
            | Q(network__tenant__project__customer__in=connected_customers)
            | Q(target_tenant__project__in=connected_projects)
            | Q(target_tenant__project__customer__in=connected_customers)
        ).distinct()

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
        summary="Create RBAC policy",
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

        event_logger.emit(
            "RBAC policy created: network {network_name} shared with {target_tenant_name} "
            "(policy type: {policy_type}).",
            event_type=EventType.OPENSTACK_RBAC_POLICY_CREATED,
            event_context={
                "rbac_policy_uuid": str(policy.uuid),
                "network": network,
                "target_tenant": target_tenant,
                "network_name": network.name,
                "target_tenant_name": target_tenant.name,
                "policy_type": policy_type,
            },
            scopes=[network, network.tenant, target_tenant, network.tenant.project],
        )

        result_serializer = self.get_serializer(policy, context={"request": request})
        return response.Response(result_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Delete RBAC policy",
        description="Delete RBAC policy for the network",
        request=None,
        responses={204: None},
    )
    def destroy(self, request, *args, **kwargs):
        policy: models.NetworkRBACPolicy = self.get_object()

        self._check_rbac_policy_permissions(
            request.user, policy.network, policy.target_tenant
        )

        network = policy.network
        target_tenant = policy.target_tenant
        policy_uuid = str(policy.uuid)
        policy_type = policy.policy_type

        backend = network.tenant.get_backend()
        backend.delete_network_rbac_policy(rbac_id=policy.backend_id)
        policy.delete()

        event_logger.emit(
            "RBAC policy removed: network {network_name} no longer shared with "
            "{target_tenant_name} (policy type: {policy_type}).",
            event_type=EventType.OPENSTACK_RBAC_POLICY_DELETED,
            event_context={
                "rbac_policy_uuid": policy_uuid,
                "network": network,
                "target_tenant": target_tenant,
                "network_name": network.name,
                "target_tenant_name": target_tenant.name,
                "policy_type": policy_type,
            },
            scopes=[network, network.tenant, target_tenant, network.tenant.project],
        )

        return response.Response(status=status.HTTP_204_NO_CONTENT)
