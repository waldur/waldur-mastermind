from unittest import mock

from neutronclient.client import exceptions as neutron_exceptions

from waldur_openstack.openstack_discovery import (
    OpenStackDiscoveryError,
    OpenStackDiscoveryService,
    OpenStackTemporaryCredentials,
)


def _make_credentials():
    return OpenStackTemporaryCredentials(
        auth_url="https://cloud.example.com:5000/v3",
        username="admin",
        password="secret",
        user_domain_name="Default",
        project_domain_name="Default",
        project_name="admin",
    )


class TestOpenStackDiscoveryServiceValidation:
    def test_validate_credentials_success(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_auth_ref = mock.MagicMock()
        mock_auth_ref.project_id = "project-123"
        mock_session.auth.get_access.return_value = mock_auth_ref

        with mock.patch(
            "waldur_openstack.openstack_discovery.create_session",
            return_value=mock_session,
        ):
            result = service.validate_credentials()

        assert result["valid"] is True
        assert result["server_info"]["project_id"] == "project-123"
        assert result["server_info"]["auth_url"] == "https://cloud.example.com:5000/v3"

    def test_validate_credentials_failure(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        with mock.patch(
            "waldur_openstack.openstack_discovery.create_session",
            side_effect=Exception("Authentication failed"),
        ):
            result = service.validate_credentials()

        assert result["valid"] is False
        assert "Authentication failed" in result["error"]

    def test_validate_credentials_ssl_error_hint(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        with mock.patch(
            "waldur_openstack.openstack_discovery.create_session",
            side_effect=Exception("SSL certificate verify failed"),
        ):
            result = service.validate_credentials()

        assert result["valid"] is False
        assert "verify_ssl" in result["error"]


class TestOpenStackDiscoveryServiceExternalNetworks:
    def test_discover_external_networks_success(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_neutron = mock.MagicMock()
        mock_neutron.list_networks.return_value = {
            "networks": [
                {
                    "id": "net-1",
                    "name": "public",
                    "shared": True,
                    "subnets": ["subnet-1", "subnet-2"],
                },
                {
                    "id": "net-2",
                    "name": "floating",
                    "shared": False,
                    "subnets": ["subnet-3"],
                },
            ]
        }
        mock_neutron.list_subnets.side_effect = [
            {
                "subnets": [
                    {
                        "id": "subnet-1",
                        "name": "public-v4",
                        "cidr": "10.0.0.0/24",
                        "gateway_ip": "10.0.0.1",
                        "ip_version": 4,
                    },
                    {
                        "id": "subnet-2",
                        "name": "public-v6",
                        "cidr": "2001:db8::/64",
                        "gateway_ip": "2001:db8::1",
                        "ip_version": 6,
                    },
                ]
            },
            {
                "subnets": [
                    {
                        "id": "subnet-3",
                        "name": "floating-subnet",
                        "cidr": "192.168.0.0/24",
                        "gateway_ip": "192.168.0.1",
                        "ip_version": 4,
                    },
                ]
            },
        ]

        with (
            mock.patch(
                "waldur_openstack.openstack_discovery.create_session",
                return_value=mock_session,
            ),
            mock.patch(
                "waldur_openstack.openstack_discovery.get_neutron_client",
                return_value=mock_neutron,
            ),
        ):
            result = service.discover_external_networks()

        assert len(result) == 2
        assert result[0]["id"] == "net-1"
        assert result[0]["name"] == "public"
        assert result[0]["is_shared"] is True
        assert len(result[0]["subnets"]) == 2
        assert result[0]["subnets"][0]["id"] == "subnet-1"
        assert result[0]["subnets"][0]["cidr"] == "10.0.0.0/24"
        mock_neutron.list_networks.assert_called_once_with(**{"router:external": True})

    def test_discover_external_networks_error(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_neutron = mock.MagicMock()
        mock_neutron.list_networks.side_effect = (
            neutron_exceptions.NeutronClientException("Network error")
        )

        with (
            mock.patch(
                "waldur_openstack.openstack_discovery.create_session",
                return_value=mock_session,
            ),
            mock.patch(
                "waldur_openstack.openstack_discovery.get_neutron_client",
                return_value=mock_neutron,
            ),
        ):
            try:
                service.discover_external_networks()
                assert False, "Expected OpenStackDiscoveryError"
            except OpenStackDiscoveryError:
                pass


class TestOpenStackDiscoveryServiceAvailabilityZones:
    def test_discover_instance_availability_zones(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_nova = mock.MagicMock()
        zone1 = mock.MagicMock()
        zone1.zoneName = "nova"
        zone1.zoneState = {"available": True}
        zone2 = mock.MagicMock()
        zone2.zoneName = "az2"
        zone2.zoneState = {"available": False}
        mock_nova.availability_zones.list.return_value = [zone1, zone2]

        with (
            mock.patch(
                "waldur_openstack.openstack_discovery.create_session",
                return_value=mock_session,
            ),
            mock.patch(
                "waldur_openstack.openstack_discovery.get_nova_client",
                return_value=mock_nova,
            ),
        ):
            result = service.discover_instance_availability_zones()

        assert len(result) == 2
        assert result[0] == {"name": "nova", "state": "available"}
        assert result[1] == {"name": "az2", "state": "unavailable"}
        mock_nova.availability_zones.list.assert_called_once_with(detailed=False)

    def test_discover_volume_availability_zones(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_cinder = mock.MagicMock()
        zone = mock.MagicMock()
        zone.zoneName = "nova"
        zone.zoneState = {"available": True}
        mock_cinder.availability_zones.list.return_value = [zone]

        with (
            mock.patch(
                "waldur_openstack.openstack_discovery.create_session",
                return_value=mock_session,
            ),
            mock.patch(
                "waldur_openstack.openstack_discovery.get_cinder_client",
                return_value=mock_cinder,
            ),
        ):
            result = service.discover_volume_availability_zones()

        assert len(result) == 1
        assert result[0] == {"name": "nova", "state": "available"}


class TestOpenStackDiscoveryServiceVolumeTypes:
    def test_discover_volume_types(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_cinder = mock.MagicMock()
        vt1 = mock.MagicMock()
        vt1.id = "vt-1"
        vt1.name = "ssd"
        vt1.description = "SSD volume"
        vt2 = mock.MagicMock()
        vt2.id = "vt-2"
        vt2.name = "hdd"
        vt2.description = None
        mock_cinder.volume_types.list.return_value = [vt1, vt2]

        with (
            mock.patch(
                "waldur_openstack.openstack_discovery.create_session",
                return_value=mock_session,
            ),
            mock.patch(
                "waldur_openstack.openstack_discovery.get_cinder_client",
                return_value=mock_cinder,
            ),
        ):
            result = service.discover_volume_types()

        assert len(result) == 2
        assert result[0] == {"id": "vt-1", "name": "ssd", "description": "SSD volume"}
        assert result[1] == {"id": "vt-2", "name": "hdd", "description": ""}


class TestOpenStackDiscoveryServiceFlavors:
    def test_discover_flavors(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        mock_session = mock.MagicMock()
        mock_nova = mock.MagicMock()
        flavor = mock.MagicMock()
        flavor.id = "f-1"
        flavor.name = "m1.small"
        flavor.vcpus = 1
        flavor.ram = 2048
        flavor.disk = 20
        mock_nova.flavors.list.return_value = [flavor]

        with (
            mock.patch(
                "waldur_openstack.openstack_discovery.create_session",
                return_value=mock_session,
            ),
            mock.patch(
                "waldur_openstack.openstack_discovery.get_nova_client",
                return_value=mock_nova,
            ),
        ):
            result = service.discover_flavors()

        assert len(result) == 1
        assert result[0] == {
            "id": "f-1",
            "name": "m1.small",
            "vcpus": 1,
            "ram": 2048,
            "disk": 20,
        }


class TestOpenStackDiscoveryServiceBuildAttributes:
    def test_build_service_attributes(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        result = service.build_service_attributes(
            external_network_id="net-1",
            instance_availability_zone="nova",
            volume_availability_zone="cinder-az1",
        )

        assert result["service_attributes"]["backend_url"] == creds.auth_url
        assert result["service_attributes"]["username"] == creds.username
        assert result["service_attributes"]["password"] == creds.password
        assert result["service_attributes"]["domain"] == creds.user_domain_name
        assert result["service_attributes"]["tenant_name"] == creds.project_name
        assert result["plugin_options"]["external_network_id"] == "net-1"
        assert result["plugin_options"]["valid_availability_zones"] == {"nova": "nova"}
        assert result["plugin_options"]["volume_availability_zone_name"] == "cinder-az1"

    def test_build_service_attributes_minimal(self):
        creds = _make_credentials()
        service = OpenStackDiscoveryService(creds)

        result = service.build_service_attributes()

        assert result["service_attributes"]["backend_url"] == creds.auth_url
        assert result["plugin_options"]["external_network_id"] == ""
        assert "valid_availability_zones" not in result["plugin_options"]
        assert "volume_availability_zone_name" not in result["plugin_options"]

    def test_build_service_attributes_with_certificate(self):
        creds = OpenStackTemporaryCredentials(
            auth_url="https://cloud.example.com:5000/v3",
            username="admin",
            password="secret",
            certificate="-----BEGIN CERTIFICATE-----\nMIID...\n-----END CERTIFICATE-----",
        )
        service = OpenStackDiscoveryService(creds)

        result = service.build_service_attributes()

        assert "certificate" in result["plugin_options"]
