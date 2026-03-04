"""
Octavia Load Balancer API client.

Uses keystoneauth session to make REST calls to the Octavia (load-balancer) API.
No additional dependencies - Octavia is not included in python-neutronclient.
"""

import logging

from keystoneauth1 import exceptions as ka_exceptions

from waldur_openstack.exceptions import OpenStackBackendError

logger = logging.getLogger(__name__)


def get_octavia_client(session):
    """
    Return a minimal Octavia API client using the keystone session.

    Octavia uses service type 'load-balancer' in the Keystone catalog.
    """
    return OctaviaClient(session)


class OctaviaClientException(Exception):
    """Wrapper for Octavia API errors."""

    def __init__(self, status_code, message, details=None):
        self.status_code = status_code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{status_code}] {message}: {details}")


class OctaviaClient:
    """
    Minimal client for Octavia Load Balancer API v2.

    API reference: https://docs.openstack.org/api-ref/load-balancer/v2/
    """

    def __init__(self, session):
        self._session = session

    def _get_base_url(self):
        try:
            endpoint = self._session.get_endpoint(
                service_type="load-balancer",
                interface="public",
            )
            return endpoint.rstrip("/")
        except ka_exceptions.catalog.EndpointNotFound:
            raise OpenStackBackendError(
                "Load balancer service (Octavia) is not available in this OpenStack deployment."
            )

    def _request(self, method, path, **kwargs):
        url = f"{self._get_base_url()}{path}"
        try:
            response = self._session.request(method, url, **kwargs)
        except ka_exceptions.ClientException as e:
            raise OpenStackBackendError(e)

        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("faultstring") or body.get("error", {}).get(
                    "message", str(body)
                )
            except Exception:
                message = response.text or response.reason

            raise OctaviaClientException(
                status_code=response.status_code,
                message=message,
                details={"url": url},
            )

        if response.status_code in (204, 202) and not response.content:
            return None

        return response.json() if response.content else None

    def create_load_balancer(self, name, vip_subnet_id, provider="ovn", **kwargs):
        """Create a load balancer. Returns the loadbalancer object."""
        payload = {
            "loadbalancer": {
                "name": name,
                "vip_subnet_id": vip_subnet_id,
                "provider": provider,
                **kwargs,
            }
        }
        result = self._request("POST", "/v2/lbaas/loadbalancers", json=payload)
        return result["loadbalancer"]

    def show_load_balancer(self, lb_id):
        """Get a single load balancer by ID."""
        result = self._request("GET", f"/v2/lbaas/loadbalancers/{lb_id}")
        return result["loadbalancer"]

    def list_load_balancers(self, project_id=None):
        """List load balancers for the project (from auth token)."""
        params = {}
        if project_id:
            params["project_id"] = project_id
        result = self._request("GET", "/v2/lbaas/loadbalancers", params=params or None)
        return result.get("loadbalancers", [])

    def update_load_balancer(self, lb_id, **kwargs):
        """Update a load balancer (e.g. name)."""
        payload = {"loadbalancer": kwargs}
        result = self._request("PUT", f"/v2/lbaas/loadbalancers/{lb_id}", json=payload)
        return result["loadbalancer"] if result else None

    def delete_load_balancer(self, lb_id):
        """Delete a load balancer (cascade deletes listeners, pools, members)."""
        self._request("DELETE", f"/v2/lbaas/loadbalancers/{lb_id}")

    # Pools

    def list_pools(self, loadbalancer_id=None, project_id=None):
        """List pools. Filter by loadbalancer_id or project_id."""
        params = {}
        if loadbalancer_id:
            params["loadbalancer_id"] = loadbalancer_id
        if project_id:
            params["project_id"] = project_id
        result = self._request("GET", "/v2/lbaas/pools", params=params or None)
        return result.get("pools", [])

    def create_pool(self, loadbalancer_id, name, protocol, lb_algorithm, **kwargs):
        """Create a pool. OVN requires lb_algorithm=SOURCE_IP_PORT."""
        payload = {
            "pool": {
                "loadbalancer_id": loadbalancer_id,
                "name": name,
                "protocol": protocol,
                "lb_algorithm": lb_algorithm,
                **kwargs,
            }
        }
        result = self._request("POST", "/v2/lbaas/pools", json=payload)
        return result["pool"]

    def show_pool(self, pool_id):
        """Get a single pool by ID."""
        result = self._request("GET", f"/v2/lbaas/pools/{pool_id}")
        return result["pool"]

    def update_pool(self, pool_id, **kwargs):
        """Update a pool (e.g. name)."""
        payload = {"pool": kwargs}
        result = self._request("PUT", f"/v2/lbaas/pools/{pool_id}", json=payload)
        return result["pool"] if result else None

    def delete_pool(self, pool_id):
        """Delete a pool (cascade deletes members, health monitor)."""
        self._request("DELETE", f"/v2/lbaas/pools/{pool_id}")

    # Pool Members

    def list_members(self, pool_id):
        """List members of a pool."""
        result = self._request("GET", f"/v2/lbaas/pools/{pool_id}/members")
        return result.get("members", [])

    def create_member(self, pool_id, address, protocol_port, subnet_id, **kwargs):
        """Create a pool member."""
        payload = {
            "member": {
                "address": address,
                "protocol_port": protocol_port,
                "subnet_id": subnet_id,
                **kwargs,
            }
        }
        result = self._request(
            "POST", f"/v2/lbaas/pools/{pool_id}/members", json=payload
        )
        return result["member"]

    def show_member(self, pool_id, member_id):
        """Get a single pool member by ID."""
        result = self._request("GET", f"/v2/lbaas/pools/{pool_id}/members/{member_id}")
        return result["member"]

    def update_member(self, pool_id, member_id, **kwargs):
        """Update a pool member (e.g. weight)."""
        payload = {"member": kwargs}
        result = self._request(
            "PUT",
            f"/v2/lbaas/pools/{pool_id}/members/{member_id}",
            json=payload,
        )
        return result["member"] if result else None

    def delete_member(self, pool_id, member_id):
        """Delete a pool member."""
        self._request("DELETE", f"/v2/lbaas/pools/{pool_id}/members/{member_id}")

    # Health Monitors

    def list_healthmonitors(self, pool_id=None, project_id=None):
        """List health monitors. Filter by pool_id or project_id."""
        params = {}
        if pool_id:
            params["pool_id"] = pool_id
        if project_id:
            params["project_id"] = project_id
        result = self._request("GET", "/v2/lbaas/healthmonitors", params=params or None)
        return result.get("healthmonitors", [])

    def create_healthmonitor(
        self, pool_id, hm_type, delay, timeout, max_retries, **kwargs
    ):
        """Create a health monitor. OVN supports TCP and UDP only."""
        payload = {
            "healthmonitor": {
                "pool_id": pool_id,
                "type": hm_type,
                "delay": delay,
                "timeout": timeout,
                "max_retries": max_retries,
                **kwargs,
            }
        }
        result = self._request("POST", "/v2/lbaas/healthmonitors", json=payload)
        return result["healthmonitor"]

    def show_healthmonitor(self, healthmonitor_id):
        """Get a single health monitor by ID."""
        result = self._request("GET", f"/v2/lbaas/healthmonitors/{healthmonitor_id}")
        return result["healthmonitor"]

    def update_healthmonitor(self, healthmonitor_id, **kwargs):
        """Update a health monitor (e.g. delay, timeout, max_retries)."""
        payload = {"healthmonitor": kwargs}
        result = self._request(
            "PUT",
            f"/v2/lbaas/healthmonitors/{healthmonitor_id}",
            json=payload,
        )
        return result["healthmonitor"] if result else None

    def delete_healthmonitor(self, healthmonitor_id):
        """Delete a health monitor."""
        self._request("DELETE", f"/v2/lbaas/healthmonitors/{healthmonitor_id}")

    # Listeners

    def list_listeners(self, loadbalancer_id=None, project_id=None):
        """List listeners. Filter by loadbalancer_id or project_id."""
        params = {}
        if loadbalancer_id:
            params["loadbalancer_id"] = loadbalancer_id
        if project_id:
            params["project_id"] = project_id
        result = self._request("GET", "/v2/lbaas/listeners", params=params or None)
        return result.get("listeners", [])

    def create_listener(self, loadbalancer_id, protocol, protocol_port, name, **kwargs):
        """Create a listener. OVN supports TCP and UDP."""
        payload = {
            "listener": {
                "loadbalancer_id": loadbalancer_id,
                "protocol": protocol,
                "protocol_port": protocol_port,
                "name": name,
                **kwargs,
            }
        }
        result = self._request("POST", "/v2/lbaas/listeners", json=payload)
        return result["listener"]

    def show_listener(self, listener_id):
        """Get a single listener by ID."""
        result = self._request("GET", f"/v2/lbaas/listeners/{listener_id}")
        return result["listener"]

    def update_listener(self, listener_id, **kwargs):
        """Update a listener (e.g. name, default_pool_id)."""
        payload = {"listener": kwargs}
        result = self._request(
            "PUT", f"/v2/lbaas/listeners/{listener_id}", json=payload
        )
        return result["listener"] if result else None

    def delete_listener(self, listener_id):
        """Delete a listener."""
        self._request("DELETE", f"/v2/lbaas/listeners/{listener_id}")
