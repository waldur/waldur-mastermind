from collections import defaultdict

from django.db.models import QuerySet
from rest_framework import serializers

from waldur_core.core.utils import pwgen
from waldur_core.core.validators import validate_name
from waldur_core.structure.models import Project, ServiceSettings
from waldur_core.structure.serializers import PermissionFieldFilteringMixin
from waldur_mastermind.marketplace.models import Offering, Plan, Resource
from waldur_mastermind.marketplace_openstack import AVAILABLE_LIMITS
from waldur_mastermind.marketplace_openstack.utils import (
    _apply_quotas,
    map_limits_to_quotas,
)
from waldur_openstack.models import (
    Network,
    SecurityGroup,
    SecurityGroupRule,
    SubNet,
    Tenant,
    VolumeType,
)
from waldur_openstack.serializers import (
    _generate_subnet_allocation_pool,
    can_create_tenant,
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
            "state",
        )

    mappings = MappingSerializer()
    state = serializers.ReadOnlyField(source="get_state_display")

    created_by_uuid = serializers.ReadOnlyField(source="created_by.uuid")
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")

    src_offering_uuid = serializers.ReadOnlyField(source="src_resource.offering.uuid")
    src_offering_name = serializers.ReadOnlyField(source="src_resource.offering.name")
    dst_offering_uuid = serializers.ReadOnlyField(source="dst_resource.offering.uuid")
    dst_offering_name = serializers.ReadOnlyField(source="dst_resource.offering.name")

    src_resource_uuid = serializers.ReadOnlyField(source="src_resource.uuid")
    src_resource_name = serializers.ReadOnlyField(source="src_resource.name")
    dst_resource_uuid = serializers.ReadOnlyField(source="dst_resource.uuid")
    dst_resource_name = serializers.ReadOnlyField(source="dst_resource.name")


class MigrationCreateSerializer(
    PermissionFieldFilteringMixin, serializers.ModelSerializer
):
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

    def get_filtered_field_names(self):
        return ("src_resource", "dst_offering", "dst_plan")

    def validate(self, attrs):
        src_resource: Resource = attrs["src_resource"]
        src_tenant: Tenant = src_resource.scope

        dst_offering: Resource = attrs["dst_offering"]
        dst_settings: ServiceSettings = dst_offering.scope
        dst_project = src_resource.project

        user = self.context["request"].user
        can_create_tenant(user, dst_settings, dst_project)

        mappings = attrs.get("mappings", {})
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
                    "Invalid dst_type_uuid %s as it is not available in service settings.",
                    dst_type_uuid,
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
        subnet_mappings = {}
        for subnet in validated_data.get("mappings", {}).get("subnets", []):
            src_cidr = subnet["src_cidr"]
            dst_cidr = subnet["dst_cidr"]
            subnet_mappings[src_cidr] = dst_cidr
        src_networks: QuerySet[Network] = src_tenant.networks.all()
        for src_network in src_networks:
            dst_network = Network.objects.create(
                name=src_network.name,
                description=src_network.description,
                service_settings=dst_settings,
                project=dst_project,
                tenant=dst_tenant,
                mtu=src_network.mtu,
            )
            src_subnets: QuerySet[SubNet] = src_network.subnets.all()
            for src_subnet in src_subnets:
                subnet_cidr = subnet_mappings.get(src_subnet.cidr) or src_subnet.cidr
                SubNet.objects.create(
                    name=src_network.name,
                    description=src_network.description,
                    service_settings=dst_settings,
                    project=dst_project,
                    tenant=dst_tenant,
                    network=dst_network,
                    cidr=subnet_cidr,
                    dns_nameservers=src_subnet.dns_nameservers,
                    host_routes=src_subnet.host_routes,
                    allocation_pools=_generate_subnet_allocation_pool(subnet_cidr),
                )
        for src_group in src_tenant.security_groups.all():
            dst_group = SecurityGroup.objects.create(
                service_settings=dst_settings,
                project=dst_project,
                tenant=dst_tenant,
                name=src_group.name,
                description=src_group.description,
            )
            for src_rule in dst_group.rules.all():
                rule_cidr = subnet_mappings.get(src_rule.cidr) or src_rule.cidr
                SecurityGroupRule.objects.create(
                    security_group=dst_group,
                    protocol=src_rule.protocol,
                    from_port=src_rule.from_port,
                    to_port=src_rule.to_port,
                    cidr=rule_cidr,
                    direction=src_rule.direction,
                    ethertype=src_rule.ethertype,
                )

    def get_limits(self, validated_data, src_resource: Resource):
        volume_type_mappings = {}
        for volume_type in validated_data.get("mappings", {}).get("volume_types", []):
            src_type_uuid = volume_type["src_type_uuid"]
            dst_type_uuid = volume_type["dst_type_uuid"]
            src_type = VolumeType.objects.get(uuid=src_type_uuid)
            dst_type = VolumeType.objects.get(uuid=dst_type_uuid)
            volume_type_mappings[src_type.name] = dst_type.name

        if volume_type_mappings:
            limits = {name: src_resource.limits.get(name) for name in AVAILABLE_LIMITS}
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
        else:
            limits = src_resource.limits
        return limits

    def create(self, validated_data):
        validated_data["created_by"] = self.context["request"].user
        src_resource: Resource = validated_data["src_resource"]

        name = validated_data.get("name") or src_resource.name
        description = validated_data.get("description") or src_resource.description
        src_tenant: Tenant = src_resource.scope

        dst_offering: Resource = validated_data.pop("dst_offering")
        dst_plan: Plan = validated_data.pop("dst_plan")
        dst_settings: ServiceSettings = dst_offering.scope
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
        migration = super().create(validated_data)
        return migration
