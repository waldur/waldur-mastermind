"""Compose a router's effective routing table from already-pulled state.

The effective routes are the union of three sources:

- **default**: derived from the router's external gateway. When the router
  has an external gateway port, there is one default route per address family
  the gateway has an address in -- `0.0.0.0/0` for IPv4, `::/0` for IPv6 --
  via that family's gateway subnet `gateway_ip`.
- **connected**: one route per attached interface — the subnet's CIDR is
  on-link via the interface port.
- **static**: each row in `router.routes` (user-set).

No Neutron calls are made; all inputs are populated by `pull_tenant_routers`.
"""

import ipaddress

from waldur_openstack import models

DEFAULT_DESTINATIONS = {4: "0.0.0.0/0", 6: "::/0"}


def _ip_version(address) -> int | None:
    """4 or 6 for an IP address, None for anything that is not one."""
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return None


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


def _default_route_row(
    router: models.Router, version: int, subnet=None, ip_on_router=None
) -> dict:
    return {
        "destination": DEFAULT_DESTINATIONS[version],
        "nexthop": getattr(subnet, "gateway_ip", None) if subnet else None,
        "source": "default",
        "subnet_uuid": str(subnet.uuid) if subnet is not None else None,
        "subnet_name": getattr(subnet, "name", "") if subnet else "",
        "subnet_cidr": getattr(subnet, "cidr", "") if subnet else "",
        "gateway_ip_on_router": ip_on_router,
        "external_network_uuid": str(router.external_network_ref.uuid)
        if router.external_network_ref_id
        else None,
        "external_network_name": router.external_network_ref.name
        if router.external_network_ref_id
        else "",
    }


def _default_routes(router: models.Router) -> list[dict]:
    if not router.has_external_gateway:
        return []

    # The gateway port holds one fixed IP per family it routes; the first
    # address of each family decides that family's default route. The address
    # itself decides the family: a subnet created by Waldur keeps the default
    # ip_version of 4 until it is pulled, so the subnet only fills in when the
    # address is missing.
    rows: list[dict] = []
    for fixed_ip in router.external_fixed_ips or []:
        _, subnet = _gateway_subnet(router, fixed_ip.get("subnet_id"))
        ip_on_router = fixed_ip.get("ip_address")
        version = _ip_version(ip_on_router) or (
            subnet.ip_version if subnet is not None else None
        )
        if version not in DEFAULT_DESTINATIONS:
            continue
        if any(row["destination"] == DEFAULT_DESTINATIONS[version] for row in rows):
            continue
        rows.append(_default_route_row(router, version, subnet, ip_on_router))
    if rows:
        return rows

    # Gateway is set but Waldur hasn't synced fixed IPs yet — emit best-effort
    # rows so the UI still tells the user the default route exists, even if
    # the next-hop is unknown. The families are taken from the external
    # network's subnets, and IPv4 is assumed only when nothing is known.
    versions: list[int] = []
    if router.external_network_ref_id:
        versions = sorted(
            set(
                router.external_network_ref.subnets.values_list("ip_version", flat=True)
            )
            & set(DEFAULT_DESTINATIONS)
        )
    return [_default_route_row(router, version) for version in versions or [4]]


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
    routes.extend(_default_routes(router))
    routes.extend(_connected_routes(router))
    routes.extend(_static_routes(router))
    return {
        "snat": router.enable_snat,
        "has_external_gateway": router.has_external_gateway,
        "routes": routes,
    }
