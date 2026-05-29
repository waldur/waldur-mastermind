"""Compose a router's effective routing table from already-pulled state.

The effective routes are the union of three sources:

- **default**: derived from the router's external gateway. When the router
  has an external gateway port, the default route is `0.0.0.0/0` via the
  gateway subnet's `gateway_ip`.
- **connected**: one route per attached interface — the subnet's CIDR is
  on-link via the interface port.
- **static**: each row in `router.routes` (user-set).

No Neutron calls are made; all inputs are populated by `pull_tenant_routers`.
"""

from waldur_openstack import models


def _gateway_subnet(router: models.Router, subnet_backend_id: str | None):
    """Resolve the subnet that backs the external gateway port.

    Tries `ExternalSubnet` first (the global external-network case), then
    falls back to a tenant `SubNet` (RBAC-shared-as-external case).
    """
    if not subnet_backend_id:
        return None, None
    if router.external_network_ref_id:
        ext_subnet = models.ExternalSubnet.objects.filter(
            network=router.external_network_ref,
            backend_id=subnet_backend_id,
        ).first()
        if ext_subnet is not None:
            return "external", ext_subnet
    subnet = models.SubNet.objects.filter(backend_id=subnet_backend_id).first()
    if subnet is not None:
        return "internal", subnet
    return None, None


def _default_route(router: models.Router) -> dict | None:
    if not router.has_external_gateway:
        return None
    fixed_ips = router.external_fixed_ips or []
    if not fixed_ips:
        # Gateway is set but Waldur hasn't synced fixed IPs yet — emit a
        # best-effort row so the UI still tells the user the default route
        # exists, even if the next-hop is unknown.
        return {
            "destination": "0.0.0.0/0",
            "nexthop": None,
            "source": "default",
            "subnet_uuid": None,
            "subnet_name": "",
            "subnet_cidr": "",
            "gateway_ip_on_router": None,
            "external_network_uuid": str(router.external_network_ref.uuid)
            if router.external_network_ref_id
            else None,
            "external_network_name": router.external_network_ref.name
            if router.external_network_ref_id
            else "",
        }
    first = fixed_ips[0]
    _, subnet = _gateway_subnet(router, first.get("subnet_id"))
    return {
        "destination": "0.0.0.0/0",
        "nexthop": getattr(subnet, "gateway_ip", None) if subnet else None,
        "source": "default",
        "subnet_uuid": str(subnet.uuid) if subnet is not None else None,
        "subnet_name": getattr(subnet, "name", "") if subnet else "",
        "subnet_cidr": getattr(subnet, "cidr", "") if subnet else "",
        "gateway_ip_on_router": first.get("ip_address"),
        "external_network_uuid": str(router.external_network_ref.uuid)
        if router.external_network_ref_id
        else None,
        "external_network_name": router.external_network_ref.name
        if router.external_network_ref_id
        else "",
    }


def _connected_routes(router: models.Router) -> list[dict]:
    rows: list[dict] = []
    for port in router.ports.select_related("subnet"):
        subnet = port.subnet
        if subnet is None:
            continue
        # The router's IP on this subnet (first fixed IP that matches the subnet).
        ip_on_router: str | None = None
        for fixed in port.fixed_ips or []:
            if fixed.get("subnet_id") == subnet.backend_id and fixed.get("ip_address"):
                ip_on_router = fixed["ip_address"]
                break
        rows.append(
            {
                "destination": subnet.cidr,
                "nexthop": None,
                "source": "connected",
                "subnet_uuid": str(subnet.uuid),
                "subnet_name": subnet.name,
                "subnet_cidr": subnet.cidr,
                "port_uuid": str(port.uuid),
                "port_backend_id": port.backend_id,
                "ip_on_router": ip_on_router,
            }
        )
    return rows


def _static_routes(router: models.Router) -> list[dict]:
    rows: list[dict] = []
    for entry in router.routes or []:
        rows.append(
            {
                "destination": entry.get("destination", ""),
                "nexthop": entry.get("nexthop"),
                "source": "static",
            }
        )
    return rows


def compute_effective_routes(router: models.Router) -> dict:
    """Compose the router's effective routing table."""
    routes: list[dict] = []
    default = _default_route(router)
    if default is not None:
        routes.append(default)
    routes.extend(_connected_routes(router))
    routes.extend(_static_routes(router))
    return {
        "snat": router.enable_snat,
        "has_external_gateway": router.has_external_gateway,
        "routes": routes,
    }
