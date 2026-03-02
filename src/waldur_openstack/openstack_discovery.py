"""
OpenStack Settings Discovery Service

This module provides stateless discovery functionality for OpenStack settings.
It creates temporary clients using provided credentials without saving them.
"""

import logging
import os
from dataclasses import dataclass

from cinderclient import exceptions as cinder_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.session import (
    create_session,
    get_certificate_filename,
    get_cinder_client,
    get_neutron_client,
    get_nova_client,
)

logger = logging.getLogger(__name__)


class OpenStackDiscoveryError(OpenStackBackendError):
    """Exception for discovery-related errors."""

    pass


@dataclass
class OpenStackTemporaryCredentials:
    """Container for temporary OpenStack credentials."""

    auth_url: str
    username: str
    password: str
    user_domain_name: str = "Default"
    project_domain_name: str = "Default"
    project_name: str = "admin"
    verify_ssl: bool = False
    certificate: str | None = None
    auth_type: str = "password"


class OpenStackDiscoveryService:
    """
    Stateless service for discovering OpenStack configuration.

    All methods accept temporary credentials and do not persist anything.
    """

    def __init__(self, credentials: OpenStackTemporaryCredentials):
        self.credentials = credentials
        self._session = None

    @property
    def session(self):
        """Lazily create authenticated Keystone session."""
        if self._session is None:
            self._session = self._create_session()
        return self._session

    def _get_verify_ssl(self):
        verify_ssl = self.credentials.verify_ssl
        if self.credentials.certificate:
            file_path = get_certificate_filename(self.credentials.certificate)
            if not os.path.isfile(file_path):
                with open(file_path, "w") as fh:
                    fh.write(self.credentials.certificate)
            return file_path
        return verify_ssl

    def _create_session(self):
        credentials = {
            "auth_url": self.credentials.auth_url,
            "username": self.credentials.username,
            "password": self.credentials.password,
            "user_domain_name": self.credentials.user_domain_name,
            "project_domain_name": self.credentials.project_domain_name,
            "project_name": self.credentials.project_name,
            "auth_type": self.credentials.auth_type,
        }
        verify_ssl = self._get_verify_ssl()
        return create_session(credentials, verify_ssl)

    def validate_credentials(self) -> dict:
        """
        Validate credentials by attempting to connect to Keystone.

        Returns:
            dict with 'valid' boolean and 'message' or 'error'
        """
        try:
            session = self.session
            auth_ref = session.auth.get_access(session)
            return {
                "valid": True,
                "message": "Credentials validated successfully",
                "server_info": {
                    "auth_url": self.credentials.auth_url,
                    "identity_api_version": "3",
                    "user_domain_name": self.credentials.user_domain_name,
                    "project_name": self.credentials.project_name,
                    "project_id": auth_ref.project_id,
                },
            }
        except Exception as e:
            logger.warning("OpenStack credential validation failed: %s", e)
            error_msg = str(e)
            if "SSL" in error_msg or "certificate" in error_msg.lower():
                error_msg += " (Hint: try setting verify_ssl to false)"
            return {
                "valid": False,
                "error": error_msg,
            }

    def discover_external_networks(self) -> list[dict]:
        """
        Discover available external networks.

        Returns:
            List of external network dictionaries.
        """
        neutron = get_neutron_client(self.session)
        try:
            networks = neutron.list_networks(**{"router:external": True})["networks"]
        except neutron_exceptions.NeutronClientException as e:
            raise OpenStackDiscoveryError(f"Failed to discover external networks: {e}")

        result = []
        for network in networks:
            subnet_ids = network.get("subnets", [])
            subnets_detail = []
            if subnet_ids:
                try:
                    remote_subnets = neutron.list_subnets(
                        id=subnet_ids,
                    )["subnets"]
                except neutron_exceptions.NeutronClientException:
                    remote_subnets = []
                for s in remote_subnets:
                    subnets_detail.append(
                        {
                            "id": s["id"],
                            "name": s.get("name", ""),
                            "cidr": s.get("cidr", ""),
                            "gateway_ip": s.get("gateway_ip", ""),
                            "ip_version": s.get("ip_version", 4),
                        }
                    )
            result.append(
                {
                    "id": network["id"],
                    "name": network.get("name", ""),
                    "is_shared": network.get("shared", False),
                    "subnets": subnets_detail,
                }
            )
        return result

    def discover_instance_availability_zones(self) -> list[dict]:
        """
        Discover available Nova instance availability zones.

        Returns:
            List of availability zone dictionaries.
        """
        nova = get_nova_client(self.session)
        try:
            zones = nova.availability_zones.list(detailed=False)
        except nova_exceptions.ClientException as e:
            raise OpenStackDiscoveryError(
                f"Failed to discover instance availability zones: {e}"
            )

        return [
            {
                "name": zone.zoneName,
                "state": "available"
                if zone.zoneState.get("available")
                else "unavailable",
            }
            for zone in zones
        ]

    def discover_volume_availability_zones(self) -> list[dict]:
        """
        Discover available Cinder volume availability zones.

        Returns:
            List of availability zone dictionaries.
        """
        cinder = get_cinder_client(self.session)
        try:
            zones = cinder.availability_zones.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackDiscoveryError(
                f"Failed to discover volume availability zones: {e}"
            )

        return [
            {
                "name": zone.zoneName,
                "state": "available"
                if zone.zoneState.get("available")
                else "unavailable",
            }
            for zone in zones
        ]

    def discover_volume_types(self) -> list[dict]:
        """
        Discover available volume types.

        Returns:
            List of volume type dictionaries.
        """
        cinder = get_cinder_client(self.session)
        try:
            volume_types = cinder.volume_types.list()
        except cinder_exceptions.ClientException as e:
            raise OpenStackDiscoveryError(f"Failed to discover volume types: {e}")

        return [
            {
                "id": vt.id,
                "name": vt.name,
                "description": getattr(vt, "description", "") or "",
            }
            for vt in volume_types
        ]

    def discover_flavors(self) -> list[dict]:
        """
        Discover available flavors.

        Returns:
            List of flavor dictionaries.
        """
        nova = get_nova_client(self.session)
        try:
            flavors = nova.flavors.list()
        except nova_exceptions.ClientException as e:
            raise OpenStackDiscoveryError(f"Failed to discover flavors: {e}")

        return [
            {
                "id": flavor.id,
                "name": flavor.name,
                "vcpus": flavor.vcpus,
                "ram": flavor.ram,
                "disk": flavor.disk,
            }
            for flavor in flavors
        ]

    def build_service_attributes(
        self,
        external_network_id: str = "",
        instance_availability_zone: str = "",
        volume_availability_zone: str = "",
    ) -> dict:
        """
        Assemble service_attributes and plugin_options dicts from selected values.

        Returns:
            dict with 'service_attributes' and 'plugin_options' keys.
        """
        service_attributes = {
            "backend_url": self.credentials.auth_url,
            "username": self.credentials.username,
            "password": self.credentials.password,
            "domain": self.credentials.user_domain_name,
            "tenant_name": self.credentials.project_name,
        }

        plugin_options = {
            "external_network_id": external_network_id,
        }

        if self.credentials.certificate:
            plugin_options["certificate"] = self.credentials.certificate

        plugin_options["verify_ssl"] = self.credentials.verify_ssl

        if instance_availability_zone:
            plugin_options["valid_availability_zones"] = {
                instance_availability_zone: instance_availability_zone
            }

        if volume_availability_zone:
            plugin_options["volume_availability_zone_name"] = volume_availability_zone

        return {
            "service_attributes": service_attributes,
            "plugin_options": plugin_options,
        }
