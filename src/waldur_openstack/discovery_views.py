from drf_spectacular.utils import extend_schema
from rest_framework import decorators, response, status
from rest_framework import exceptions as rf_exceptions

from waldur_core.core.serializers import EmptySerializer
from waldur_core.core.views import ActionsViewSet
from waldur_mastermind.marketplace.models import ServiceProvider
from waldur_openstack import discovery_serializers as serializers
from waldur_openstack.openstack_discovery import (
    OpenStackDiscoveryError,
    OpenStackDiscoveryService,
    OpenStackTemporaryCredentials,
)


class OpenStackDiscoveryViewSet(ActionsViewSet):
    """
    ViewSet for OpenStack settings discovery.

    Allows authorized users to:
    1. Validate OpenStack credentials without saving
    2. Discover available infrastructure (networks, AZs, volume types, flavors)
    3. Preview service_attributes for offering creation
    """

    queryset = ServiceProvider.objects.none()  # Stateless operations
    serializer_class = EmptySerializer

    def is_authorized_for_discovery(request, view, obj=None):
        """Staff users or owners of a customer that is a service provider."""
        if request.user.is_staff:
            return
        from waldur_core.permissions.fixtures import CustomerRole
        from waldur_core.permissions.utils import has_user

        for sp in ServiceProvider.objects.all():
            if has_user(sp.customer, request.user, CustomerRole.OWNER):
                return
        raise rf_exceptions.PermissionDenied()

    def _get_discovery_service(self, credentials_data: dict):
        """Create discovery service from validated credentials."""
        creds = OpenStackTemporaryCredentials(
            auth_url=credentials_data["auth_url"],
            username=credentials_data["username"],
            password=credentials_data["password"],
            user_domain_name=credentials_data.get("user_domain_name", "Default"),
            project_domain_name=credentials_data.get("project_domain_name", "Default"),
            project_name=credentials_data.get("project_name", "admin"),
            verify_ssl=credentials_data.get("verify_ssl", False),
            certificate=credentials_data.get("certificate"),
        )
        return OpenStackDiscoveryService(creds)

    @extend_schema(
        request=serializers.OpenStackCredentialsSerializer,
        responses={200: serializers.CredentialsValidationResponseSerializer},
        description="Validate OpenStack credentials without saving them.",
    )
    @decorators.action(detail=False, methods=["post"])
    def validate_credentials(self, request):
        """Validate OpenStack credentials without saving."""
        serializer = serializers.OpenStackCredentialsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        result = service.validate_credentials()

        return response.Response(result, status=status.HTTP_200_OK)

    validate_credentials_serializer_class = serializers.OpenStackCredentialsSerializer
    validate_credentials_permissions = [is_authorized_for_discovery]

    @extend_schema(
        request=serializers.DiscoverExternalNetworksRequestSerializer,
        responses={200: serializers.ExternalNetworkResponseSerializer(many=True)},
        description="Discover available external networks.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_external_networks(self, request):
        """Discover available external networks."""
        serializer = serializers.DiscoverExternalNetworksRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            networks = service.discover_external_networks()
        except OpenStackDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.ExternalNetworkResponseSerializer(
            networks, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_external_networks_serializer_class = (
        serializers.DiscoverExternalNetworksRequestSerializer
    )
    discover_external_networks_permissions = [is_authorized_for_discovery]

    @extend_schema(
        request=serializers.DiscoverInstanceAvailabilityZonesRequestSerializer,
        responses={200: serializers.AvailabilityZoneResponseSerializer(many=True)},
        description="Discover available Nova instance availability zones.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_instance_availability_zones(self, request):
        """Discover available instance availability zones."""
        serializer = serializers.DiscoverInstanceAvailabilityZonesRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            zones = service.discover_instance_availability_zones()
        except OpenStackDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.AvailabilityZoneResponseSerializer(
            zones, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_instance_availability_zones_serializer_class = (
        serializers.DiscoverInstanceAvailabilityZonesRequestSerializer
    )
    discover_instance_availability_zones_permissions = [is_authorized_for_discovery]

    @extend_schema(
        request=serializers.DiscoverVolumeAvailabilityZonesRequestSerializer,
        responses={200: serializers.AvailabilityZoneResponseSerializer(many=True)},
        description="Discover available Cinder volume availability zones.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_volume_availability_zones(self, request):
        """Discover available volume availability zones."""
        serializer = serializers.DiscoverVolumeAvailabilityZonesRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            zones = service.discover_volume_availability_zones()
        except OpenStackDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.AvailabilityZoneResponseSerializer(
            zones, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_volume_availability_zones_serializer_class = (
        serializers.DiscoverVolumeAvailabilityZonesRequestSerializer
    )
    discover_volume_availability_zones_permissions = [is_authorized_for_discovery]

    @extend_schema(
        request=serializers.DiscoverVolumeTypesRequestSerializer,
        responses={200: serializers.VolumeTypeResponseSerializer(many=True)},
        description="Discover available volume types.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_volume_types(self, request):
        """Discover available volume types."""
        serializer = serializers.DiscoverVolumeTypesRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            volume_types = service.discover_volume_types()
        except OpenStackDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.VolumeTypeResponseSerializer(
            volume_types, many=True
        )
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_volume_types_serializer_class = (
        serializers.DiscoverVolumeTypesRequestSerializer
    )
    discover_volume_types_permissions = [is_authorized_for_discovery]

    @extend_schema(
        request=serializers.DiscoverFlavorsRequestSerializer,
        responses={200: serializers.FlavorResponseSerializer(many=True)},
        description="Discover available flavors.",
    )
    @decorators.action(detail=False, methods=["post"])
    def discover_flavors(self, request):
        """Discover available flavors."""
        serializer = serializers.DiscoverFlavorsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)
        try:
            flavors = service.discover_flavors()
        except OpenStackDiscoveryError as e:
            return response.Response(
                {"error": str(e)}, status=status.HTTP_400_BAD_REQUEST
            )

        response_serializer = serializers.FlavorResponseSerializer(flavors, many=True)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    discover_flavors_serializer_class = serializers.DiscoverFlavorsRequestSerializer
    discover_flavors_permissions = [is_authorized_for_discovery]

    @extend_schema(
        request=serializers.PreviewServiceAttributesRequestSerializer,
        responses={200: serializers.ServiceAttributesPreviewSerializer},
        description="Build service_attributes and plugin_options from selected values.",
    )
    @decorators.action(detail=False, methods=["post"])
    def preview_service_attributes(self, request):
        """Build service_attributes and plugin_options from credential and selection data."""
        serializer = serializers.PreviewServiceAttributesRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        service = self._get_discovery_service(serializer.validated_data)

        # Validate credentials first
        validation = service.validate_credentials()
        if not validation.get("valid"):
            return response.Response(
                {
                    "valid": False,
                    "error": validation.get("error", "Invalid credentials"),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = service.build_service_attributes(
            external_network_id=serializer.validated_data.get(
                "external_network_id", ""
            ),
            instance_availability_zone=serializer.validated_data.get(
                "instance_availability_zone", ""
            ),
            volume_availability_zone=serializer.validated_data.get(
                "volume_availability_zone", ""
            ),
        )

        response_serializer = serializers.ServiceAttributesPreviewSerializer(result)
        return response.Response(response_serializer.data, status=status.HTTP_200_OK)

    preview_service_attributes_serializer_class = (
        serializers.PreviewServiceAttributesRequestSerializer
    )
    preview_service_attributes_permissions = [is_authorized_for_discovery]
