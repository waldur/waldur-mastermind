from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace_openstack.utils import _apply_quotas
from waldur_openstack import models as openstack_models
from waldur_openstack import serializers as openstack_serializers

from .utils import get_external_ip


class MarketplaceTenantCreateSerializer(
    openstack_serializers.OpenStackTenantSerializer
):
    quotas = serializers.JSONField(required=False, default=dict)
    skip_connection_extnet = serializers.BooleanField(default=False)
    skip_creation_of_default_router = serializers.BooleanField(default=False)
    skip_creation_of_default_subnet = serializers.BooleanField(default=False)
    mtu = serializers.IntegerField(min_value=68, max_value=9000, required=False)

    class Meta(openstack_serializers.OpenStackTenantSerializer.Meta):
        fields = openstack_serializers.OpenStackTenantSerializer.Meta.fields + (
            "skip_connection_extnet",
            "skip_creation_of_default_router",
            "skip_creation_of_default_subnet",
            "quotas",
            "mtu",
        )

    @transaction.atomic
    def create(self, validated_data):
        quotas = validated_data.pop("quotas")
        tenant = super().create(validated_data)
        if quotas:
            _apply_quotas(tenant, quotas)
        return tenant

    def _validate_service_settings(self, service_settings, project):
        pass


def get_marketplace_resource_uuid(serializer, volume) -> str | None:
    try:
        resource = marketplace_models.Resource.objects.filter(scope=volume).get()
        return resource.uuid.hex
    except marketplace_models.Resource.DoesNotExist:
        return


def add_marketplace_resource_uuid(sender, fields, **kwargs):
    """Add a marketplace resource UUID field to the serializer."""
    fields["marketplace_resource_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_uuid", get_marketplace_resource_uuid)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.OpenStackNestedVolumeSerializer,
    receiver=add_marketplace_resource_uuid,
)


def get_router_external_ips(serializer, router) -> list[str] | None:
    try:
        if not (router.fixed_ips or router.tenant):
            return

        resource = marketplace_models.Resource.objects.filter(scope=router.tenant).get()
        external_ips = []

        for router_fixed_ip in router.fixed_ips:
            external_ip = get_external_ip(resource.offering, router_fixed_ip)
            external_ip and external_ips.append(external_ip)

        return external_ips
    except marketplace_models.Resource.DoesNotExist:
        return


def add_router_external_ips(sender, fields, **kwargs):
    """Add router external IPs to the serializer."""
    fields["offering_external_ips"] = serializers.SerializerMethodField()
    setattr(sender, "get_offering_external_ips", get_router_external_ips)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.OpenStackRouterSerializer,
    receiver=add_router_external_ips,
)


@extend_schema_field(serializers.BooleanField())
def get_config_drive_default(serializer, offering: marketplace_models.Offering) -> bool:
    try:
        service = offering.scope
    except AttributeError:
        return False
    if isinstance(service, structure_models.BaseResource):
        service = service.service_settings
    if not isinstance(service, structure_models.ServiceSettings):
        return False
    return bool(service.options.get("config_drive", False))


def add_openstack_config_drive_default(sender, fields, **kwargs):
    """Expose the OpenStack-wide config_drive default on public offering details.

    Kept here (not in marketplace core) so that OpenStack-specific concerns
    do not leak into the generic marketplace offering serializer.
    """
    fields["config_drive_default"] = serializers.SerializerMethodField()
    setattr(sender, "get_config_drive_default", get_config_drive_default)


core_signals.pre_serializer_fields.connect(
    sender=marketplace_serializers.PublicOfferingDetailsSerializer,
    receiver=add_openstack_config_drive_default,
)


class TenantSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = openstack_models.Tenant
        fields = (
            "url",
            "uuid",
            "name",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "openstack-marketplace-tenant-detail",
            },
        }


class ImageCreateSerializer(serializers.Serializer):
    DISK_FORMATS = [
        "qcow2",
        "raw",
        "vhd",
        "vmdk",
        "vdi",
        "iso",
        "aki",
        "ami",
        "ari",
    ]

    CONTAINER_FORMATS = [
        "bare",
        "ovf",
        "aki",
        "ami",
        "ari",
    ]

    name = serializers.CharField()
    min_ram = serializers.IntegerField(required=False, default=0)
    min_disk = serializers.IntegerField(required=False, default=0)
    disk_format = serializers.ChoiceField(
        choices=[(format, format) for format in DISK_FORMATS], default="qcow2"
    )
    container_format = serializers.ChoiceField(
        choices=[(format, format) for format in CONTAINER_FORMATS], default="bare"
    )
    visibility = serializers.ChoiceField(
        choices=[(format, format) for format in ["private", "public"]],
        default="private",
    )

    def validate_visibility(self, value):
        public_images_is_available = self.context.get("public_images_is_available")
        if value == "public" and not public_images_is_available:
            raise serializers.ValidationError(
                _("You do not have permissions to create public images.")
            )
        return value


class ImageCreateResponseSerializer(serializers.Serializer):
    image_id = serializers.UUIDField()
    name = serializers.CharField()
    status = serializers.CharField()
    upload_url = serializers.CharField()


class ImageUploadResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    message = serializers.CharField()


class DuplicateOfferingCandidateSerializer(serializers.Serializer):
    """One offering in a duplicate per-tenant group (read-only diagnostics)."""

    id = serializers.IntegerField()
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    state = serializers.CharField()
    active_resources = serializers.IntegerField()
    total_resources = serializers.IntegerField()
    is_recommended_keeper = serializers.BooleanField()


class DuplicateOfferingGroupSerializer(serializers.Serializer):
    """A tenant + offering type that has more than one per-tenant offering.

    Shape produced by ``utils.build_duplicate_offering_report``; surfaces the
    duplicate offerings, the recommended keeper and the count of orphaned
    resources so a staff user can see the problem without reading logs.
    """

    tenant_id = serializers.IntegerField()
    tenant_uuid = serializers.UUIDField(allow_null=True)
    tenant_name = serializers.CharField(allow_null=True)
    customer_name = serializers.CharField(allow_null=True)
    customer_uuid = serializers.UUIDField(allow_null=True)
    offering_type = serializers.CharField()
    recommended_keeper_id = serializers.IntegerField()
    orphan_count = serializers.IntegerField()
    candidates = DuplicateOfferingCandidateSerializer(many=True)
