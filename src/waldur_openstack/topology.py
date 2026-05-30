from waldur_openstack import models


def _node(node_id, type_, name, *, uuid=None, attrs=None):
    return {
        "id": node_id,
        "type": type_,
        "name": name,
        "uuid": str(uuid) if uuid is not None else None,
        "attrs": attrs or {},
    }


def _edge(source, target, kind):
    return {"source": source, "target": target, "kind": kind}


def build_tenant_topology(tenant: models.Tenant) -> dict:
    """Compose nodes + edges for a tenant's network topology.

    All data is read from already-pulled state — no Neutron calls.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    tenant_id = f"tenant:{tenant.uuid.hex}"
    nodes.append(
        _node(
            tenant_id,
            "tenant",
            tenant.name,
            uuid=tenant.uuid,
            attrs={"backend_id": tenant.backend_id or ""},
        )
    )

    # External networks referenced by this tenant or its routers.
    seen_external: dict[int, str] = {}

    def ensure_external(ext_net: models.ExternalNetwork | None) -> str | None:
        if ext_net is None:
            return None
        if ext_net.pk in seen_external:
            return seen_external[ext_net.pk]
        node_id = f"external_network:{ext_net.uuid.hex}"
        nodes.append(
            _node(
                node_id,
                "external_network",
                ext_net.name,
                uuid=ext_net.uuid,
                attrs={
                    "backend_id": ext_net.backend_id,
                    "is_shared": ext_net.is_shared,
                    "is_default": ext_net.is_default,
                },
            )
        )
        seen_external[ext_net.pk] = node_id
        return node_id

    if tenant.external_network_ref_id:
        ext_id = ensure_external(tenant.external_network_ref)
        if ext_id:
            edges.append(_edge(tenant_id, ext_id, "gateway"))

    # Networks owned by the tenant.
    network_node_ids: dict[int, str] = {}
    for network in tenant.networks.all():
        node_id = f"network:{network.uuid.hex}"
        network_node_ids[network.pk] = node_id
        nodes.append(
            _node(
                node_id,
                "network",
                network.name,
                uuid=network.uuid,
                attrs={
                    "is_external": network.is_external,
                    "backend_id": network.backend_id,
                    "mtu": network.mtu,
                    "type": network.type,
                },
            )
        )
        edges.append(_edge(tenant_id, node_id, "contains"))

    # Subnets per network.
    subnet_node_ids: dict[int, str] = {}
    for subnet in models.SubNet.objects.filter(network_id__in=network_node_ids.keys()):
        node_id = f"subnet:{subnet.uuid.hex}"
        subnet_node_ids[subnet.pk] = node_id
        nodes.append(
            _node(
                node_id,
                "subnet",
                subnet.name,
                uuid=subnet.uuid,
                attrs={
                    "cidr": subnet.cidr,
                    "gateway_ip": subnet.gateway_ip,
                    "is_connected": subnet.is_connected,
                    "ip_version": subnet.ip_version,
                    "backend_id": subnet.backend_id,
                },
            )
        )
        net_node = network_node_ids.get(subnet.network_id)
        if net_node:
            edges.append(_edge(net_node, node_id, "has_subnet"))

    # Instances.
    instance_node_ids: dict[int, str] = {}
    for instance in models.Instance.objects.filter(tenant=tenant):
        node_id = f"instance:{instance.uuid.hex}"
        instance_node_ids[instance.pk] = node_id
        nodes.append(
            _node(
                node_id,
                "instance",
                instance.name,
                uuid=instance.uuid,
                attrs={
                    "runtime_state": instance.runtime_state,
                    "state": instance.state,
                    "backend_id": instance.backend_id,
                    "flavor_name": instance.flavor_name,
                },
            )
        )
        edges.append(_edge(tenant_id, node_id, "contains"))

    # Ports — connect to subnet and instance (if any).
    port_node_ids: dict[int, str] = {}
    for port in models.Port.objects.filter(tenant=tenant).select_related(
        "subnet", "instance"
    ):
        node_id = f"port:{port.uuid.hex}"
        port_node_ids[port.pk] = node_id
        nodes.append(
            _node(
                node_id,
                "port",
                port.name or f"port-{port.backend_id[:8]}",
                uuid=port.uuid,
                attrs={
                    "backend_id": port.backend_id,
                    "fixed_ips": port.fixed_ips,
                    "mac_address": port.mac_address,
                },
            )
        )
        if port.subnet_id and port.subnet_id in subnet_node_ids:
            edges.append(_edge(subnet_node_ids[port.subnet_id], node_id, "has_port"))
        if port.instance_id and port.instance_id in instance_node_ids:
            edges.append(
                _edge(node_id, instance_node_ids[port.instance_id], "attached_to")
            )

    # Routers — connect via Router.ports M2M and to external network.
    for router in tenant.routers.all().prefetch_related("ports"):
        node_id = f"router:{router.uuid.hex}"
        nodes.append(
            _node(
                node_id,
                "router",
                router.name,
                uuid=router.uuid,
                attrs={
                    "backend_id": router.backend_id,
                    "has_external_gateway": router.has_external_gateway,
                    "enable_snat": router.enable_snat,
                    "external_fixed_ips": router.external_fixed_ips,
                },
            )
        )
        edges.append(_edge(tenant_id, node_id, "contains"))
        for port in router.ports.all():
            if port.pk in port_node_ids:
                edges.append(_edge(node_id, port_node_ids[port.pk], "has_interface"))
        if router.external_network_ref_id:
            ext_id = ensure_external(router.external_network_ref)
            if ext_id:
                edges.append(_edge(node_id, ext_id, "gateway"))

    # Floating IPs.
    for fip in models.FloatingIP.objects.filter(tenant=tenant).select_related("port"):
        node_id = f"floating_ip:{fip.uuid.hex}"
        nodes.append(
            _node(
                node_id,
                "floating_ip",
                fip.address or fip.name,
                uuid=fip.uuid,
                attrs={
                    "address": fip.address,
                    "external_address": fip.external_address,
                    "backend_network_id": fip.backend_network_id,
                    "runtime_state": fip.runtime_state,
                },
            )
        )
        edges.append(_edge(tenant_id, node_id, "contains"))
        if fip.port_id and fip.port_id in port_node_ids:
            edges.append(_edge(node_id, port_node_ids[fip.port_id], "floating_for"))

    # Inbound RBAC shares (other tenants' networks shared with us).
    inbound_policies = models.NetworkRBACPolicy.objects.filter(
        target_tenant=tenant
    ).select_related("network", "network__tenant")
    for policy in inbound_policies:
        share_id = f"rbac_share:{policy.uuid.hex}"
        src_network = policy.network
        nodes.append(
            _node(
                share_id,
                "rbac_share",
                src_network.name,
                uuid=policy.uuid,
                attrs={
                    "policy_type": policy.policy_type,
                    "source_network_uuid": src_network.uuid.hex,
                    "source_tenant_name": src_network.tenant.name
                    if src_network.tenant_id
                    else "",
                    "backend_id": policy.backend_id,
                },
            )
        )
        edges.append(_edge(share_id, tenant_id, "shared_with"))

    return {"nodes": nodes, "edges": edges}
