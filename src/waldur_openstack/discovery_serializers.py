from rest_framework import serializers

# --- Request serializers ---


class OpenStackCredentialsSerializer(serializers.Serializer):
    """Serializer for OpenStack credentials - accepts temporary credentials."""

    auth_url = serializers.URLField(
        required=True,
        help_text="Keystone auth URL (e.g., https://cloud.example.com:5000/v3)",
    )
    username = serializers.CharField(required=True)
    password = serializers.CharField(required=True, write_only=True)
    user_domain_name = serializers.CharField(
        default="Default",
        help_text="Keystone user domain name",
    )
    project_domain_name = serializers.CharField(
        default="Default",
        help_text="Keystone project domain name",
    )
    project_name = serializers.CharField(
        default="admin",
        help_text="Keystone project (tenant) name",
    )
    verify_ssl = serializers.BooleanField(default=False)
    certificate = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        help_text="PEM-encoded CA certificate for SSL verification",
    )


class DiscoverExternalNetworksRequestSerializer(OpenStackCredentialsSerializer):
    """Request serializer for external network discovery - credentials only."""

    pass


class DiscoverInstanceAvailabilityZonesRequestSerializer(
    OpenStackCredentialsSerializer
):
    """Request serializer for instance availability zone discovery."""

    pass


class DiscoverVolumeAvailabilityZonesRequestSerializer(OpenStackCredentialsSerializer):
    """Request serializer for volume availability zone discovery."""

    pass


class DiscoverVolumeTypesRequestSerializer(OpenStackCredentialsSerializer):
    """Request serializer for volume type discovery."""

    pass


class DiscoverFlavorsRequestSerializer(OpenStackCredentialsSerializer):
    """Request serializer for flavor discovery."""

    pass


class PreviewServiceAttributesRequestSerializer(OpenStackCredentialsSerializer):
    """Request serializer for building service_attributes from selections."""

    external_network_id = serializers.CharField(
        required=False,
        default="",
        help_text="Selected external network ID",
    )
    instance_availability_zone = serializers.CharField(
        required=False,
        default="",
        help_text="Selected instance availability zone name",
    )
    volume_availability_zone = serializers.CharField(
        required=False,
        default="",
        help_text="Selected volume availability zone name",
    )


# --- Response serializers ---


class ServerInfoSerializer(serializers.Serializer):
    """Server info returned on successful credential validation."""

    auth_url = serializers.CharField()
    identity_api_version = serializers.CharField()
    user_domain_name = serializers.CharField()
    project_name = serializers.CharField()
    project_id = serializers.CharField(allow_blank=True)


class CredentialsValidationResponseSerializer(serializers.Serializer):
    """Response serializer for credential validation."""

    valid = serializers.BooleanField()
    message = serializers.CharField(required=False, allow_blank=True)
    error = serializers.CharField(required=False, allow_blank=True)
    server_info = ServerInfoSerializer(required=False, allow_null=True)


class ExternalNetworkSubnetResponseSerializer(serializers.Serializer):
    """Response serializer for subnets within a discovered external network."""

    id = serializers.CharField()
    name = serializers.CharField()
    cidr = serializers.CharField(allow_blank=True)
    gateway_ip = serializers.CharField(allow_blank=True)
    ip_version = serializers.IntegerField()


class ExternalNetworkResponseSerializer(serializers.Serializer):
    """Response serializer for discovered external networks."""

    id = serializers.CharField()
    name = serializers.CharField()
    is_shared = serializers.BooleanField()
    subnets = ExternalNetworkSubnetResponseSerializer(many=True)


class AvailabilityZoneResponseSerializer(serializers.Serializer):
    """Response serializer for discovered availability zones."""

    name = serializers.CharField()
    state = serializers.CharField()


class VolumeTypeResponseSerializer(serializers.Serializer):
    """Response serializer for discovered volume types."""

    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True, required=False)


class FlavorResponseSerializer(serializers.Serializer):
    """Response serializer for discovered flavors."""

    id = serializers.CharField()
    name = serializers.CharField()
    vcpus = serializers.IntegerField()
    ram = serializers.IntegerField(help_text="RAM in MB")
    disk = serializers.IntegerField(help_text="Disk in GB")


class ServiceAttributesPreviewSerializer(serializers.Serializer):
    """Response serializer for the assembled service_attributes + plugin_options."""

    service_attributes = serializers.DictField()
    plugin_options = serializers.DictField()
