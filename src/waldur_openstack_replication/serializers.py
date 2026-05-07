from collections import defaultdict
from typing import cast

from django.db import transaction
from netaddr import IPNetwork
from rest_framework import serializers

from waldur_core.core.utils import pwgen
from waldur_core.core.validators import validate_name
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Project, ServiceSettings
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace.models import Offering, Order, Plan, Resource
from waldur_mastermind.marketplace.permissions import (
    order_should_not_be_reviewed_by_consumer,
)
from waldur_mastermind.marketplace.serializers import validate_plan
from waldur_mastermind.marketplace_openstack import AVAILABLE_LIMITS
from waldur_mastermind.marketplace_openstack.utils import (
    _apply_quotas,
    map_limits_to_quotas,
)
from waldur_openstack.models import (
    Network,
    Router,
    SecurityGroup,
    SecurityGroupRule,
    SubNet,
    Tenant,
    VolumeType,
)
from waldur_openstack.serializers import (
    _generate_subnet_allocation_pool,
    validate_private_subnet_cidr,
)
from waldur_openstack.utils import (
    is_valid_volume_type_name,
    volume_type_name_to_quota_name,
)

from . import models


class VolumeTypeMappingSerializer(serializers.Serializer):
    src_type_uuid = serializers.UUIDField()
    dst_type_uuid = serializers.UUIDField()


class SubNetMappingSerializer(serializers.Serializer):
    src_cidr = serializers.CharField(validators=[validate_private_subnet_cidr])
    dst_cidr = serializers.CharField(validators=[validate_private_subnet_cidr])


class MappingSerializer(serializers.Serializer):
    volume_types = VolumeTypeMappingSerializer(many=True, required=False)
    subnets = SubNetMappingSerializer(many=True, required=False)
    skip_connection_extnet = serializers.BooleanField(required=False, default=False)
    sync_instance_ports = serializers.BooleanField(required=False, default=False)
    networks = serializers.SlugRelatedField(
        queryset=Network.objects.all(), slug_field="uuid", required=False, many=True
    )


class MigrationDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Migration
        fields = (
            "uuid",
            "created",
            "modified",
            "mappings",
            "created_by_uuid",
            "created_by_full_name",
            "src_offering_uuid",
            "src_offering_name",
            "dst_offering_uuid",
            "dst_offering_name",
            "src_resource_uuid",
            "src_resource_name",
            "dst_resource_uuid",
            "dst_resource_name",
            "dst_resource_state",
            "state",
            "error_message",
            "error_traceback",
        )

    mappings = MappingSerializer()
    state = serializers.CharField(read_only=True, source="get_state_display")

    created_by_uuid = serializers.UUIDField(read_only=True, source="created_by.uuid")
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")

    src_offering_uuid = serializers.UUIDField(
        read_only=True, source="src_resource.offering.uuid"
    )
    src_offering_name = serializers.ReadOnlyField(source="src_resource.offering.name")
    dst_offering_uuid = serializers.UUIDField(
        read_only=True, source="dst_resource.offering.uuid"
    )
    dst_offering_name = serializers.ReadOnlyField(source="dst_resource.offering.name")

    src_resource_uuid = serializers.UUIDField(
        read_only=True, source="src_resource.uuid"
    )
    src_resource_name = serializers.ReadOnlyField(source="src_resource.name")
    dst_resource_uuid = serializers.UUIDField(
        read_only=True, source="dst_resource.uuid"
    )
    dst_resource_name = serializers.ReadOnlyField(source="dst_resource.name")
    dst_resource_state = serializers.CharField(
        read_only=True, source="dst_resource.get_state_display"
    )


class MigrationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Migration
        fields = (
            "name",
            "description",
            "mappings",
            "src_resource",
            "dst_offering",
            "dst_plan",
        )

    name = serializers.CharField(
        write_only=True, required=False, validators=[validate_name]
    )
    description = serializers.CharField(write_only=True, required=False)
    src_resource = serializers.SlugRelatedField(
        queryset=Resource.objects.all(), slug_field="uuid"
    )
    dst_offering = serializers.SlugRelatedField(
        queryset=Offering.objects.all(), slug_field="uuid", write_only=True
    )
    dst_plan = serializers.SlugRelatedField(
        queryset=Plan.objects.all(), slug_field="uuid", write_only=True
    )
    mappings = MappingSerializer(required=False)

    def get_fields(self):
        fields = super().get_fields()

        request = self.context["request"]

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        user = request.user
        fields["src_resource"].queryset = filter_queryset_for_user(
            fields["src_resource"].queryset, user
        )
        fields["dst_offering"].queryset = fields[
            "dst_offering"
        ].queryset.filter_by_ordering_availability_for_user(user)
        return fields

    def validate(self, attrs):
        src_resource: Resource = attrs["src_resource"]
        if not src_resource.limits:
            raise serializers.ValidationError(
                {"Source resource does not have limits set."}
            )
        src_tenant = cast(Tenant, src_resource.scope)

        dst_offering: Resource = attrs["dst_offering"]
        dst_settings = cast(ServiceSettings, dst_offering.scope)
        dst_project = src_resource.project

        dst_plan: Plan = attrs.get("dst_plan")
        if dst_plan:
            if dst_plan.offering != dst_offering:
                raise serializers.ValidationError(
                    {"dst_plan": "This plan is not available for selected offering."}
                )

            validate_plan(dst_plan)

        user = self.context["request"].user
        order = Order(
            project=dst_project,
            offering=dst_offering,
            created_by=user,
            type=OrderTypes.CREATE,
        )
        if not order_should_not_be_reviewed_by_consumer(order):
            raise serializers.ValidationError(
                "User does not have enough permissions to migrate resource.",
            )

        mappings = attrs.get("mappings", {})

        # Check that sync_instance_ports cannot be used together with networks or subnets
        if mappings.get("sync_instance_ports") and mappings.get("subnets"):
            raise serializers.ValidationError(
                "sync_instance_ports cannot be used together with subnets mappings."
            )

        for volume_type_mapping in mappings.get("volume_types", []):
            src_type_uuid = volume_type_mapping["src_type_uuid"]
            dst_type_uuid = volume_type_mapping["dst_type_uuid"]

            src_type = VolumeType.objects.get(uuid=src_type_uuid)
            dst_type = VolumeType.objects.get(uuid=dst_type_uuid)

            if not src_tenant.volume_types.filter(id=src_type.id).exists():
                raise serializers.ValidationError(
                    "Invalid src_type_uuid %s as it is not available in tenant.",
                    src_type_uuid,
                )

            if dst_type.settings != dst_settings:
                raise serializers.ValidationError(
                    f"Invalid dst_type_uuid {dst_type_uuid} as it is not available in service settings.",
                )
        return attrs

    def connect_networks(
        self,
        validated_data,
        src_tenant: Tenant,
        dst_tenant: Tenant,
        dst_settings: ServiceSettings,
        dst_project: Project,
    ):
        network_uuids = [
            network.uuid.hex
            for network in validated_data.get("mappings", {}).pop("networks", [])
        ]
        src_networks = Network.objects.filter(tenant=src_tenant)
        if network_uuids:
            src_networks = src_networks.filter(uuid__in=network_uuids)
        subnet_mappings = {}
        for subnet in validated_data.get("mappings", {}).get("subnets", []):
            src_cidr = subnet["src_cidr"]
            dst_cidr = subnet["dst_cidr"]
            subnet_mappings[src_cidr] = dst_cidr
        for src_network in src_networks:
            dst_network = Network.objects.create(
                name=src_network.name,
                description=src_network.description,
                service_settings=dst_settings,
                project=dst_project,
                tenant=dst_tenant,
                mtu=src_network.mtu,
            )
            src_subnets = src_network.subnets.all()
            for src_subnet in src_subnets:
                subnet_cidr = subnet_mappings.get(src_subnet.cidr) or src_subnet.cidr
                SubNet.objects.create(
                    name=src_subnet.name,
                    description=src_subnet.description,
                    service_settings=dst_settings,
                    project=dst_project,
                    tenant=dst_tenant,
                    network=dst_network,
                    cidr=subnet_cidr,
                    disable_gateway=src_subnet.disable_gateway,
                    dns_nameservers=src_subnet.dns_nameservers,
                    host_routes=src_subnet.host_routes,
                    allocation_pools=subnet_mappings.get(src_subnet.cidr)
                    and _generate_subnet_allocation_pool(subnet_cidr)
                    or src_subnet.allocation_pools,
                )
        group_map: dict[str, SecurityGroup] = {}
        rules_map: dict[str, SecurityGroupRule] = {}
        for src_group in src_tenant.security_groups.all():
            dst_group = SecurityGroup.objects.create(
                service_settings=dst_settings,
                project=dst_project,
                tenant=dst_tenant,
                name=src_group.name,
                description=src_group.description,
            )
            group_map[src_group.id] = dst_group
            for src_rule in src_group.rules.all():
                rule_cidr = subnet_mappings.get(src_rule.cidr) or src_rule.cidr
                dst_rule = SecurityGroupRule.objects.create(
                    security_group=dst_group,
                    protocol=src_rule.protocol,
                    from_port=src_rule.from_port,
                    to_port=src_rule.to_port,
                    cidr=rule_cidr,
                    direction=src_rule.direction,
                    ethertype=src_rule.ethertype,
                )
                rules_map[src_rule.id] = dst_rule
        for src_rule in SecurityGroupRule.objects.filter(
            security_group__tenant=src_tenant
        ).exclude(remote_group__isnull=True):
            dst_rule = rules_map.get(src_rule.id)
            if dst_rule:
                dst_group = group_map.get(src_rule.remote_group.id)
                if dst_group:
                    dst_rule.remote_group = dst_group
                    dst_rule.save(update_fields=["remote_group"])
        valid_subnet_cidrs = [
            IPNetwork(cidr)
            for cidr in SubNet.objects.filter(tenant=dst_tenant).values_list(
                "cidr", flat=True
            )
        ]
        src_routers = src_tenant.routers.all()
        for src_router in src_routers:
            routes = []
            for route in src_router.routes:
                destination = IPNetwork(route["destination"])
                if any(destination in cidr for cidr in valid_subnet_cidrs):
                    routes.append(route)
            Router.objects.create(
                name=src_router.name,
                description=src_router.description,
                service_settings=dst_settings,
                project=dst_project,
                tenant=dst_tenant,
                routes=routes,
            )

    def get_limits(self, validated_data, src_resource: Resource):
        volume_type_mappings = {}
        for volume_type in validated_data.get("mappings", {}).get("volume_types", []):
            src_type_uuid = volume_type["src_type_uuid"]
            dst_type_uuid = volume_type["dst_type_uuid"]
            src_type = VolumeType.objects.get(uuid=src_type_uuid)
            dst_type = VolumeType.objects.get(uuid=dst_type_uuid)
            volume_type_mappings[src_type.name] = dst_type.name

        limits = {name: src_resource.limits.get(name) for name in AVAILABLE_LIMITS}

        if volume_type_mappings:
            volume_type_quotas = defaultdict(int)
            for key, value in src_resource.limits.items():
                if not is_valid_volume_type_name(key):
                    continue
                if not value:
                    continue
                _, name = key.split("_", 1)
                if name in volume_type_mappings:
                    key = volume_type_name_to_quota_name(volume_type_mappings.get(name))
                volume_type_quotas[key] += value
            limits.update(volume_type_quotas)

        limits = {k: v for k, v in limits.items() if v is not None}
        return limits

    @transaction.atomic
    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        src_resource: Resource = validated_data["src_resource"]

        name = validated_data.pop("name", None) or src_resource.name
        description = validated_data.get("description") or src_resource.description
        src_tenant = cast(Tenant, src_resource.scope)

        dst_offering: Offering = validated_data.pop("dst_offering")
        dst_plan: Plan = validated_data.pop("dst_plan")
        dst_settings = cast(ServiceSettings, dst_offering.scope)
        dst_project = src_resource.project

        dst_tenant = Tenant.objects.create(
            service_settings=dst_settings,
            project=dst_project,
            name=name,
            description=description,
            user_username=Tenant.generate_username(name),
            user_password=pwgen(),
        )
        self.connect_networks(
            validated_data,
            src_tenant,
            dst_tenant,
            dst_settings,
            dst_project,
        )

        limits = self.get_limits(validated_data, src_resource)
        quotas = map_limits_to_quotas(limits, dst_offering)
        for quota_name in (
            "instances",
            "volumes",
            "snapshots",
            "security_group_count",
            "security_group_rule_count",
        ):
            quotas[quota_name] = src_tenant.get_quota_limit(quota_name)

        _apply_quotas(dst_tenant, quotas)

        dst_resource = Resource.objects.create(
            project=src_resource.project,
            name=name,
            description=description,
            offering=dst_offering,
            plan=dst_plan,
            scope=dst_tenant,
            limits=limits,
        )
        validated_data["dst_resource"] = dst_resource
        validated_data.setdefault("mappings", {})
        migration = super().create(validated_data)
        return migration
