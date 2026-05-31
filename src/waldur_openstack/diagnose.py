"""Connectivity-diagnose check engine for OpenStack instances.

Given an Instance and an optional target (external / internal:<ip> /
fip:<addr>), walk the wiring that connects this instance to that
target and report ``{check, status, detail}`` rows. All checks read
from Waldur's already-pulled state — no live OpenStack round-trip.

Status values: ``ok`` (passes), ``warn`` (operational concern but not
a blocker for the named target), ``fail`` (target unreachable until
this is fixed), ``skip`` (check not applicable to this target).
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Literal

from waldur_openstack import models

try:  # pragma: no cover - integration tested in the T4 MR
    from waldur_openstack import external_network_usage  # type: ignore[attr-defined]
except ImportError:  # T4 (WAL-9974) is a sibling MR — the diagnose check
    # for pool capacity gracefully degrades if the helper isn't available.
    external_network_usage = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CheckStatus = Literal["ok", "warn", "fail", "skip"]


@dataclass
class CheckResult:
    check: str
    status: CheckStatus
    detail: str
    fix_hint: str = ""


@dataclass
class DiagnoseReport:
    target: str
    target_address: str | None
    checks: list[CheckResult]
    root_cause: str | None


_RUNNING_RUNTIME_STATES = {
    models.Instance.RuntimeStates.ACTIVE,
}


def _parse_target(target: str) -> tuple[str, str | None]:
    """Return (kind, address). ``kind`` is ``external|internal|fip``."""
    if not target or target == "external":
        return "external", None
    if ":" in target:
        kind, _, addr = target.partition(":")
        if kind in ("internal", "fip") and addr:
            return kind, addr
    return "external", None


def _check_instance_running(instance: models.Instance) -> CheckResult:
    if instance.runtime_state in _RUNNING_RUNTIME_STATES:
        return CheckResult(
            check="instance_running",
            status="ok",
            detail=f"Instance is {instance.runtime_state}.",
        )
    return CheckResult(
        check="instance_running",
        status="fail",
        detail=(
            f"Instance runtime_state is '{instance.runtime_state or 'UNKNOWN'}'."
            " Start the instance before diagnosing connectivity."
        ),
        fix_hint="Start the instance.",
    )


def _check_ports_admin_up(instance: models.Instance) -> CheckResult:
    ports = list(instance.ports.all())
    if not ports:
        return CheckResult(
            check="port_admin_state",
            status="fail",
            detail="Instance has no ports attached.",
            fix_hint="Attach the instance to a network.",
        )
    bad = [p for p in ports if p.admin_state_up is False]
    if bad:
        names = ", ".join(p.name or p.backend_id or str(p.uuid) for p in bad)
        return CheckResult(
            check="port_admin_state",
            status="fail",
            detail=f"Ports are administratively down: {names}.",
            fix_hint="Re-enable the port (Set admin state up).",
        )
    return CheckResult(
        check="port_admin_state",
        status="ok",
        detail=f"{len(ports)} port(s) admin-up.",
    )


def _check_port_security(instance: models.Instance) -> CheckResult:
    insecure_ports = []
    for port in instance.ports.all():
        if port.port_security_enabled and port.security_groups.count() == 0:
            insecure_ports.append(port)
    if insecure_ports:
        names = ", ".join(p.name or p.backend_id or str(p.uuid) for p in insecure_ports)
        return CheckResult(
            check="port_security_groups",
            status="warn",
            detail=(
                f"Port security is enabled but no security groups are bound: "
                f"{names}. All ingress / egress is denied."
            ),
            fix_hint="Attach a security group or disable port security.",
        )
    return CheckResult(
        check="port_security_groups",
        status="ok",
        detail="Each port has at least one security group or has port security disabled.",
    )


def _find_router_for_port(port: models.Port) -> models.Router | None:
    if port.subnet_id is None:
        return None
    return (
        models.Router.objects.filter(
            tenant=port.tenant,
            ports__subnet=port.subnet,
        )
        .distinct()
        .first()
    )


def _check_subnet_on_router(
    instance: models.Instance, target_kind: str
) -> tuple[CheckResult, list[models.Router]]:
    routers: list[models.Router] = []
    missing = []
    has_direct_external = False
    for port in instance.ports.all():
        if port.subnet_id is None:
            continue
        # A port directly attached to an external network reaches the
        # internet without traversing a tenant router.
        if getattr(port.network, "is_external", False):
            has_direct_external = True
            continue
        router = _find_router_for_port(port)
        if router is None:
            missing.append(port)
        else:
            routers.append(router)

    if target_kind == "internal":
        # Internal targets only need the source instance's port to exist.
        return (
            CheckResult(
                check="subnet_on_router",
                status="skip",
                detail="Not required for internal targets.",
            ),
            routers,
        )

    if has_direct_external and not routers and not missing:
        return (
            CheckResult(
                check="subnet_on_router",
                status="ok",
                detail=(
                    "Instance is directly attached to an external network; "
                    "no tenant router required."
                ),
            ),
            routers,
        )

    if missing and not routers:
        return (
            CheckResult(
                check="subnet_on_router",
                status="fail",
                detail=(
                    "No subnet of this instance is attached to a router. "
                    "External / floating-IP traffic cannot leave the tenant."
                ),
                fix_hint="Attach the subnet to a router.",
            ),
            routers,
        )
    if missing and routers:
        return (
            CheckResult(
                check="subnet_on_router",
                status="warn",
                detail=(
                    f"{len(missing)} of {len(missing) + len(routers)} subnets are "
                    "not attached to any router."
                ),
            ),
            routers,
        )
    return (
        CheckResult(
            check="subnet_on_router",
            status="ok",
            detail=f"Reachable via {len(routers)} router(s).",
        ),
        routers,
    )


def _check_router_has_gateway(
    routers: list[models.Router], target_kind: str
) -> CheckResult:
    if not routers:
        return CheckResult(
            check="router_external_gateway",
            status="skip",
            detail="No router on the path — covered by the subnet_on_router check.",
        )
    if target_kind == "internal":
        return CheckResult(
            check="router_external_gateway",
            status="skip",
            detail="Not required for internal targets.",
        )
    with_gw = [
        r
        for r in routers
        if r.external_network_id or r.external_network_ref_id or r.external_fixed_ips
    ]
    if not with_gw:
        return CheckResult(
            check="router_external_gateway",
            status="fail",
            detail=(
                "Router(s) on the path have no external gateway configured. "
                "Set an external gateway to allow outbound traffic."
            ),
            fix_hint="Set external gateway on the router.",
        )
    return CheckResult(
        check="router_external_gateway",
        status="ok",
        detail=f"{len(with_gw)} of {len(routers)} router(s) carry an external gateway.",
    )


def _check_pool_capacity(routers: list[models.Router]) -> CheckResult:
    networks = {
        r.external_network_ref for r in routers if r.external_network_ref_id is not None
    }
    if not networks:
        return CheckResult(
            check="external_pool_capacity",
            status="skip",
            detail="No external gateway network identified.",
        )
    if external_network_usage is None:
        return CheckResult(
            check="external_pool_capacity",
            status="skip",
            detail="Pool-capacity reporter not available in this build.",
        )
    worst: tuple[float, str] | None = None
    for network in networks:
        report = external_network_usage.compute_external_network_usage(network)
        for subnet in report.subnets:
            if subnet.total_capacity == 0:
                continue
            if worst is None or subnet.utilisation > worst[0]:
                worst = (subnet.utilisation, f"{network.name}/{subnet.name}")
    if worst is None:
        return CheckResult(
            check="external_pool_capacity",
            status="skip",
            detail="No measurable allocation pool on the gateway network.",
        )
    util, label = worst
    if util >= 1.0:
        return CheckResult(
            check="external_pool_capacity",
            status="fail",
            detail=f"Gateway subnet {label} is exhausted (100% used).",
            fix_hint="Free up floating IPs or add another allocation range.",
        )
    if util >= 0.9:
        return CheckResult(
            check="external_pool_capacity",
            status="warn",
            detail=f"Gateway subnet {label} is {util:.0%} used.",
        )
    return CheckResult(
        check="external_pool_capacity",
        status="ok",
        detail=f"Worst gateway pool utilisation: {util:.0%} ({label}).",
    )


def _check_floating_ip_path(
    instance: models.Instance, target_kind: str, target_addr: str | None
) -> CheckResult:
    if target_kind != "fip":
        # Informational: show whether the instance has any FIP at all.
        port_ids = list(instance.ports.values_list("id", flat=True))
        fips = list(models.FloatingIP.objects.filter(port_id__in=port_ids))
        if not fips:
            return CheckResult(
                check="floating_ip_path",
                status="skip",
                detail="Instance has no floating IP attached.",
            )
        return CheckResult(
            check="floating_ip_path",
            status="ok",
            detail=(
                "Instance has "
                f"{len(fips)} floating IP(s) attached: "
                + ", ".join(f.address or "?" for f in fips)
                + "."
            ),
        )
    if not target_addr:
        return CheckResult(
            check="floating_ip_path",
            status="fail",
            detail="Target FIP address missing.",
        )
    port_ids = list(instance.ports.values_list("id", flat=True))
    match = models.FloatingIP.objects.filter(
        port_id__in=port_ids,
        address=target_addr,
    ).first()
    if not match:
        return CheckResult(
            check="floating_ip_path",
            status="fail",
            detail=(
                f"Floating IP {target_addr} is not attached to any port of this instance."
            ),
            fix_hint="Attach the FIP to one of the instance's ports.",
        )
    return CheckResult(
        check="floating_ip_path",
        status="ok",
        detail=f"Floating IP {target_addr} is mapped to this instance.",
    )


def _check_internal_target(
    instance: models.Instance, target_kind: str, target_addr: str | None
) -> CheckResult:
    if target_kind != "internal" or not target_addr:
        return CheckResult(
            check="internal_target_reachable",
            status="skip",
            detail="Only applicable to internal targets.",
        )
    try:
        target_ip = ipaddress.ip_address(target_addr)
    except ValueError:
        return CheckResult(
            check="internal_target_reachable",
            status="fail",
            detail=f"Target address {target_addr!r} is not a valid IP.",
        )
    # Walk subnets reachable from the instance; same-subnet → ok, same router
    # network → ok (router will route), otherwise → fail.
    instance_subnets = {
        port.subnet for port in instance.ports.all() if port.subnet_id is not None
    }
    for subnet in instance_subnets:
        if not subnet.cidr:
            continue
        try:
            if target_ip in ipaddress.ip_network(subnet.cidr):
                return CheckResult(
                    check="internal_target_reachable",
                    status="ok",
                    detail=(
                        f"Target {target_addr} is in the same subnet ({subnet.cidr})."
                    ),
                )
        except ValueError:
            continue
    return CheckResult(
        check="internal_target_reachable",
        status="warn",
        detail=(
            f"Target {target_addr} is not in any of the instance's subnets. "
            "Routing depends on router configuration; assume reachability via the gateway."
        ),
    )


def _derive_root_cause(checks: list[CheckResult]) -> str | None:
    for c in checks:
        if c.status == "fail":
            return c.detail
    for c in checks:
        if c.status == "warn":
            return c.detail
    return None


def run_diagnose(instance: models.Instance, target: str = "external") -> DiagnoseReport:
    """Run the full check sequence against ``instance`` for ``target``.

    ``target`` accepts:

    - ``"external"`` (default) — outbound to the internet.
    - ``"internal:<ip>"`` — east-west to another tenant IP.
    - ``"fip:<addr>"`` — verify a specific floating-IP mapping.
    """
    kind, addr = _parse_target(target)

    checks: list[CheckResult] = []
    checks.append(_check_instance_running(instance))
    checks.append(_check_ports_admin_up(instance))
    checks.append(_check_port_security(instance))

    subnet_check, routers = _check_subnet_on_router(instance, kind)
    checks.append(subnet_check)
    checks.append(_check_router_has_gateway(routers, kind))
    checks.append(_check_pool_capacity(routers))
    checks.append(_check_floating_ip_path(instance, kind, addr))
    checks.append(_check_internal_target(instance, kind, addr))

    return DiagnoseReport(
        target=target or "external",
        target_address=addr,
        checks=checks,
        root_cause=_derive_root_cause(checks),
    )
