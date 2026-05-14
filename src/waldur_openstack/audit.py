"""Audit-log helpers for OpenStack firewall-like resources.

These helpers produce the structured payloads consumed by event-context JSON
columns. They intentionally avoid Django model instances in the output so the
payload is safe for serialization, queue transport, and SIEM ingestion.
"""

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_openstack import models

SECURITY_GROUP_RULE_COMPARE_FIELDS = [
    "ethertype",
    "direction",
    "protocol",
    "from_port",
    "to_port",
    "cidr",
    "description",
    "remote_group_uuid",
]


def serialize_security_group_rule(rule: models.SecurityGroupRule) -> dict:
    """Render a SecurityGroupRule into the audit-log shape (no FK ids)."""
    remote_group = rule.remote_group
    return {
        "ethertype": rule.ethertype,
        "direction": rule.direction,
        "protocol": rule.protocol,
        "from_port": rule.from_port,
        "to_port": rule.to_port,
        "cidr": rule.cidr,
        "description": rule.description,
        "remote_group_uuid": str(remote_group.uuid) if remote_group else None,
        "remote_group_name": remote_group.name if remote_group else None,
    }


def snapshot_security_group_rules(security_group: models.SecurityGroup) -> list[dict]:
    """Take an audit-safe snapshot of a security group's current rules.

    Each entry carries the rule's pk under ``_pk`` for diff matching; the diff
    helper strips this field via the ``serialize`` callable before payload
    emission.
    """
    return [
        {"_pk": rule.pk, **serialize_security_group_rule(rule)}
        for rule in security_group.rules.select_related("remote_group").all()
    ]


ALLOWED_ADDRESS_PAIR_COMPARE_FIELDS = ["ip_address", "mac_address"]


def serialize_allowed_address_pair(pair: dict) -> dict:
    """Normalize a pair to {ip_address, mac_address}; mac is optional in input."""
    return {
        "ip_address": pair.get("ip_address"),
        "mac_address": pair.get("mac_address"),
    }


def emit_port_security_toggled(
    port: models.Port,
    *,
    enabled: bool,
) -> None:
    event_logger.emit(
        'Port security was {state} on port "{port_name}".'.format(
            state="enabled" if enabled else "disabled",
            port_name="{port_name}",
        ),
        event_type=(
            EventType.OPENSTACK_PORT_SECURITY_ENABLED
            if enabled
            else EventType.OPENSTACK_PORT_SECURITY_DISABLED
        ),
        event_context={"port": port, "enabled": enabled},
        scopes=[port, port.tenant, port.project],
    )


def emit_allowed_address_pairs_changed(
    port: models.Port,
    old_pairs: list[dict],
    new_pairs: list[dict],
) -> None:
    """Emit aggregate diff for an allowed_address_pairs replacement.

    Pairs are matched by ip_address (the practical identity in OpenStack); a
    same-IP-different-MAC change therefore appears as a modification, not
    add+remove.
    """
    from waldur_core.logging.diff import compute_collection_diff

    diff = compute_collection_diff(
        [serialize_allowed_address_pair(p) for p in old_pairs],
        [serialize_allowed_address_pair(p) for p in new_pairs],
        identity_key=lambda p: p.get("ip_address"),
        compare_fields=ALLOWED_ADDRESS_PAIR_COMPARE_FIELDS,
        serialize=lambda p: p,
    )
    summary = diff["summary"]
    if not (summary["added"] or summary["removed"] or summary["modified"]):
        return

    event_logger.emit(
        'Allowed address pairs on port "{port_name}" changed: '
        "{added_count} added, {removed_count} removed, "
        "{modified_count} modified.",
        event_type=EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED,
        event_context={
            "port": port,
            "added_pairs": diff["added"],
            "removed_pairs": diff["removed"],
            "modified_pairs": diff["modified"],
            "added_count": summary["added"],
            "removed_count": summary["removed"],
            "modified_count": summary["modified"],
            "trigger": "user_action",
        },
        scopes=[port, port.tenant, port.project],
    )


def serialize_load_balancer(lb: models.LoadBalancer) -> dict:
    return {
        "name": lb.name,
        "vip_address": lb.vip_address,
        "vip_subnet_uuid": str(lb.vip_subnet.uuid) if lb.vip_subnet else None,
    }


def serialize_listener(listener: models.Listener) -> dict:
    return {
        "name": listener.name,
        "protocol": listener.protocol,
        "protocol_port": listener.protocol_port,
        "load_balancer_uuid": str(listener.load_balancer.uuid),
    }


def serialize_pool(pool: models.Pool) -> dict:
    return {
        "name": pool.name,
        "protocol": pool.protocol,
        "lb_algorithm": pool.lb_algorithm,
        "load_balancer_uuid": str(pool.load_balancer.uuid),
    }


def serialize_pool_member(member: models.PoolMember) -> dict:
    return {
        "address": member.address,
        "protocol_port": member.protocol_port,
        "pool_uuid": str(member.pool.uuid),
        "load_balancer_uuid": str(member.pool.load_balancer.uuid),
    }


# (model class) -> (event-type prefix, serializer, scope-builder)
_LBAAS_AUDIT_CONFIG = {
    models.LoadBalancer: (
        "OPENSTACK_LOAD_BALANCER_",
        serialize_load_balancer,
        lambda lb: [lb, lb.tenant, lb.project],
    ),
    models.Listener: (
        "OPENSTACK_LISTENER_",
        serialize_listener,
        lambda listener: [
            listener,
            listener.load_balancer,
            listener.load_balancer.tenant,
            listener.load_balancer.project,
        ],
    ),
    models.Pool: (
        "OPENSTACK_POOL_",
        serialize_pool,
        lambda pool: [
            pool,
            pool.load_balancer,
            pool.load_balancer.tenant,
            pool.load_balancer.project,
        ],
    ),
    models.PoolMember: (
        "OPENSTACK_POOL_MEMBER_",
        serialize_pool_member,
        lambda member: [
            member,
            member.pool,
            member.pool.load_balancer,
            member.pool.load_balancer.tenant,
            member.pool.load_balancer.project,
        ],
    ),
}


def emit_lbaas_lifecycle_event(
    obj,
    action: str,
    *,
    old_payload: dict | None = None,
) -> None:
    """Emit a lifecycle event (created/updated/deleted) for an LBaaS object.

    ``action`` is one of ``"created"``, ``"updated"``, ``"deleted"``. For
    ``updated``, ``old_payload`` carries the pre-change snapshot so callers
    can include the diff in the context. The current state is serialized
    fresh from ``obj``.
    """
    prefix, serializer, scope_builder = _LBAAS_AUDIT_CONFIG[type(obj)]
    event_type = getattr(EventType, f"{prefix}{action.upper()}")
    new_payload = serializer(obj)
    context = {
        "name": obj.name,
        "uuid": str(obj.uuid),
        "trigger": "user_action",
        "new": new_payload,
    }
    if action == "updated" and old_payload is not None:
        changed_fields = sorted(
            k for k in new_payload if new_payload[k] != old_payload.get(k)
        )
        if not changed_fields:
            return  # nothing meaningful changed
        context["old"] = old_payload
        context["changed_fields"] = changed_fields
        context["changed_fields_str"] = ", ".join(changed_fields)

    message_template = {
        "created": "{name} created.",
        "updated": "{name} updated ({changed_fields_str}).",
        "deleted": "{name} deleted.",
    }[action]

    event_logger.emit(
        message_template,
        event_type=event_type,
        event_context=context,
        scopes=scope_builder(obj),
    )


def emit_security_group_rules_changed(
    security_group: models.SecurityGroup,
    diff: dict,
    trigger: str,
) -> None:
    """Emit the aggregate openstack_security_group_rules_changed event.

    No-op when the diff is empty so we don't generate noise on noop API calls
    or unchanged pulls.
    """
    summary = diff["summary"]
    if not (summary["added"] or summary["removed"] or summary["modified"]):
        return

    event_logger.emit(
        'Security group "{security_group_name}" rules changed: '
        "{added_count} added, {removed_count} removed, "
        "{modified_count} modified.",
        event_type=EventType.OPENSTACK_SECURITY_GROUP_RULES_CHANGED,
        event_context={
            "security_group": security_group,
            "added_rules": diff["added"],
            "removed_rules": diff["removed"],
            "modified_rules": diff["modified"],
            "added_count": summary["added"],
            "removed_count": summary["removed"],
            "modified_count": summary["modified"],
            "trigger": trigger,
        },
        scopes=[
            security_group,
            security_group.tenant,
            security_group.project,
        ],
    )


def serialize_security_group_ref(sg: models.SecurityGroup) -> dict:
    """Minimal attachment payload — uuid is the identity, name is for display."""
    return {"uuid": str(sg.uuid), "name": sg.name}


def _diff_security_group_attachments(old_sgs, new_sgs) -> tuple[list[dict], list[dict]]:
    """Compute (added, removed) for an SG-attachment change keyed by SG uuid.

    Attachment is a pure set operation — there is no per-element field to
    'modify', so we don't reuse compute_collection_diff here.
    """
    old_by_uuid = {str(sg.uuid): sg for sg in old_sgs}
    new_by_uuid = {str(sg.uuid): sg for sg in new_sgs}
    added = [
        serialize_security_group_ref(new_by_uuid[u])
        for u in new_by_uuid
        if u not in old_by_uuid
    ]
    removed = [
        serialize_security_group_ref(old_by_uuid[u])
        for u in old_by_uuid
        if u not in new_by_uuid
    ]
    return added, removed


def emit_instance_security_groups_changed(
    instance: models.Instance,
    old_sgs,
    new_sgs,
) -> None:
    """Emit aggregate event when an instance's security-group set changes."""
    added, removed = _diff_security_group_attachments(old_sgs, new_sgs)
    if not added and not removed:
        return

    event_logger.emit(
        'Security groups on instance "{instance_name}" changed: '
        "{added_count} added, {removed_count} removed.",
        event_type=EventType.OPENSTACK_INSTANCE_SECURITY_GROUPS_CHANGED,
        event_context={
            "instance": instance,
            "added_security_groups": added,
            "removed_security_groups": removed,
            "added_count": len(added),
            "removed_count": len(removed),
            "trigger": "user_action",
        },
        scopes=[instance, instance.tenant, instance.project],
    )


def emit_port_security_groups_changed(
    port: models.Port,
    old_sgs,
    new_sgs,
) -> None:
    """Emit aggregate event when a port's security-group set changes."""
    added, removed = _diff_security_group_attachments(old_sgs, new_sgs)
    if not added and not removed:
        return

    event_logger.emit(
        'Security groups on port "{port_name}" changed: '
        "{added_count} added, {removed_count} removed.",
        event_type=EventType.OPENSTACK_PORT_SECURITY_GROUPS_CHANGED,
        event_context={
            "port": port,
            "added_security_groups": added,
            "removed_security_groups": removed,
            "added_count": len(added),
            "removed_count": len(removed),
            "trigger": "user_action",
        },
        scopes=[port, port.tenant, port.project],
    )


def emit_load_balancer_security_groups_changed(
    load_balancer: models.LoadBalancer,
    old_sgs,
    new_sgs,
) -> None:
    """Emit aggregate event for an LB security-group change (on its VIP port)."""
    added, removed = _diff_security_group_attachments(old_sgs, new_sgs)
    if not added and not removed:
        return

    event_logger.emit(
        'Security groups on load balancer "{load_balancer_name}" changed: '
        "{added_count} added, {removed_count} removed.",
        event_type=EventType.OPENSTACK_LOAD_BALANCER_SECURITY_GROUPS_CHANGED,
        event_context={
            "load_balancer": load_balancer,
            "added_security_groups": added,
            "removed_security_groups": removed,
            "added_count": len(added),
            "removed_count": len(removed),
            "trigger": "user_action",
        },
        scopes=[load_balancer, load_balancer.tenant, load_balancer.project],
    )
