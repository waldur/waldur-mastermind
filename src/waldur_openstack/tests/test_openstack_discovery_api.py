from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

BASE_URL = "/api/openstack/discovery"


class TestOpenStackDiscoveryPermissions(test.APITestCase):
    """Test permission checks for the OpenStack discovery endpoints."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory(is_staff=False)
        self.sp_owner = structure_factories.UserFactory(is_staff=False)

        # Create a customer that is a service provider, and make sp_owner its owner
        self.customer = structure_factories.CustomerFactory()
        marketplace_factories.ServiceProviderFactory(customer=self.customer)
        add_user(self.customer, self.sp_owner, CustomerRole.OWNER)

    def test_staff_can_validate_credentials(self):
        self.client.force_authenticate(self.staff_user)
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True, "message": "OK"}
            response = self.client.post(
                f"{BASE_URL}/validate_credentials/",
                {
                    "auth_url": "https://cloud.example.com:5000/v3",
                    "username": "admin",
                    "password": "secret",
                },
            )
        assert response.status_code == status.HTTP_200_OK

    def test_service_provider_owner_can_validate_credentials(self):
        self.client.force_authenticate(self.sp_owner)
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True, "message": "OK"}
            response = self.client.post(
                f"{BASE_URL}/validate_credentials/",
                {
                    "auth_url": "https://cloud.example.com:5000/v3",
                    "username": "admin",
                    "password": "secret",
                },
            )
        assert response.status_code == status.HTTP_200_OK

    def test_regular_user_cannot_validate_credentials(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.post(
            f"{BASE_URL}/validate_credentials/",
            {
                "auth_url": "https://cloud.example.com:5000/v3",
                "username": "admin",
                "password": "secret",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_anonymous_user_cannot_validate_credentials(self):
        response = self.client.post(
            f"{BASE_URL}/validate_credentials/",
            {
                "auth_url": "https://cloud.example.com:5000/v3",
                "username": "admin",
                "password": "secret",
            },
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestOpenStackDiscoveryValidation(test.APITestCase):
    """Test input validation for the OpenStack discovery endpoints."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff_user)

    def test_validate_credentials_requires_auth_url(self):
        response = self.client.post(
            f"{BASE_URL}/validate_credentials/",
            {
                "username": "admin",
                "password": "secret",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "auth_url" in response.data

    def test_validate_credentials_requires_username(self):
        response = self.client.post(
            f"{BASE_URL}/validate_credentials/",
            {
                "auth_url": "https://cloud.example.com:5000/v3",
                "password": "secret",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "username" in response.data

    def test_validate_credentials_requires_password(self):
        response = self.client.post(
            f"{BASE_URL}/validate_credentials/",
            {
                "auth_url": "https://cloud.example.com:5000/v3",
                "username": "admin",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "password" in response.data


class TestOpenStackDiscoveryExternalNetworks(test.APITestCase):
    """Test the external networks discovery endpoint."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff_user)
        self.credentials = {
            "auth_url": "https://cloud.example.com:5000/v3",
            "username": "admin",
            "password": "secret",
        }

    def test_discover_external_networks_success(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.discover_external_networks"
        ) as mock_discover:
            mock_discover.return_value = [
                {
                    "id": "net-1",
                    "name": "public",
                    "is_shared": True,
                    "subnets": [
                        {
                            "id": "subnet-1",
                            "name": "public-subnet",
                            "cidr": "10.0.0.0/24",
                            "gateway_ip": "10.0.0.1",
                            "ip_version": 4,
                        }
                    ],
                },
            ]
            response = self.client.post(
                f"{BASE_URL}/discover_external_networks/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["id"] == "net-1"
        assert response.data[0]["name"] == "public"
        assert response.data[0]["is_shared"] is True

    def test_discover_external_networks_error(self):
        from waldur_openstack.openstack_discovery import OpenStackDiscoveryError

        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.discover_external_networks"
        ) as mock_discover:
            mock_discover.side_effect = OpenStackDiscoveryError("Connection refused")
            response = self.client.post(
                f"{BASE_URL}/discover_external_networks/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "error" in response.data


class TestOpenStackDiscoveryAvailabilityZones(test.APITestCase):
    """Test the availability zone discovery endpoints."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff_user)
        self.credentials = {
            "auth_url": "https://cloud.example.com:5000/v3",
            "username": "admin",
            "password": "secret",
        }

    def test_discover_instance_availability_zones_success(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.discover_instance_availability_zones"
        ) as mock_discover:
            mock_discover.return_value = [
                {"name": "nova", "state": "available"},
            ]
            response = self.client.post(
                f"{BASE_URL}/discover_instance_availability_zones/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["name"] == "nova"

    def test_discover_volume_availability_zones_success(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.discover_volume_availability_zones"
        ) as mock_discover:
            mock_discover.return_value = [
                {"name": "nova", "state": "available"},
            ]
            response = self.client.post(
                f"{BASE_URL}/discover_volume_availability_zones/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1


class TestOpenStackDiscoveryVolumeTypes(test.APITestCase):
    """Test the volume type discovery endpoint."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff_user)
        self.credentials = {
            "auth_url": "https://cloud.example.com:5000/v3",
            "username": "admin",
            "password": "secret",
        }

    def test_discover_volume_types_success(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.discover_volume_types"
        ) as mock_discover:
            mock_discover.return_value = [
                {"id": "vt-1", "name": "ssd", "description": "SSD volume"},
            ]
            response = self.client.post(
                f"{BASE_URL}/discover_volume_types/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["name"] == "ssd"


class TestOpenStackDiscoveryFlavors(test.APITestCase):
    """Test the flavor discovery endpoint."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff_user)
        self.credentials = {
            "auth_url": "https://cloud.example.com:5000/v3",
            "username": "admin",
            "password": "secret",
        }

    def test_discover_flavors_success(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.discover_flavors"
        ) as mock_discover:
            mock_discover.return_value = [
                {
                    "id": "f-1",
                    "name": "m1.small",
                    "vcpus": 1,
                    "ram": 2048,
                    "disk": 20,
                },
            ]
            response = self.client.post(
                f"{BASE_URL}/discover_flavors/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["name"] == "m1.small"
        assert response.data[0]["vcpus"] == 1


class TestOpenStackDiscoveryPreview(test.APITestCase):
    """Test the preview_service_attributes endpoint."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff_user)
        self.credentials = {
            "auth_url": "https://cloud.example.com:5000/v3",
            "username": "admin",
            "password": "secret",
        }

    def test_preview_service_attributes_success(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {"valid": True, "message": "OK"}
            response = self.client.post(
                f"{BASE_URL}/preview_service_attributes/",
                {
                    **self.credentials,
                    "external_network_id": "net-1",
                    "instance_availability_zone": "nova",
                },
            )

        assert response.status_code == status.HTTP_200_OK
        assert "service_attributes" in response.data
        assert "plugin_options" in response.data
        assert (
            response.data["service_attributes"]["backend_url"]
            == "https://cloud.example.com:5000/v3"
        )
        assert response.data["plugin_options"]["external_network_id"] == "net-1"

    def test_preview_service_attributes_invalid_credentials(self):
        with mock.patch(
            "waldur_openstack.openstack_discovery.OpenStackDiscoveryService.validate_credentials"
        ) as mock_validate:
            mock_validate.return_value = {
                "valid": False,
                "error": "Authentication failed",
            }
            response = self.client.post(
                f"{BASE_URL}/preview_service_attributes/",
                self.credentials,
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["valid"] is False
