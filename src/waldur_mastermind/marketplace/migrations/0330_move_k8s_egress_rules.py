from functools import reduce
from operator import or_

from django.db import migrations
from django.db.models import Q

# Frozen rather than imported: a migration must not change when app code does.
K8S_CONFIG_FIELD_TYPES = ("single_datacenter_k8s_config", "multi_datacenter_k8s_config")
INGRESS_RULE_KEYS = ("public_access_rules", "administrative_access_rules")
EGRESS_RULES_KEY = "egress_rules"
# Where the reverse puts egress rules back; the order form used to add new
# rows of either direction to any table.
REVERSE_TARGET_KEY = "public_access_rules"
KEPT_RULE_KEYS = (
    "uuid",
    "id",
    "name",
    "protocol",
    "port_range_min",
    "port_range_max",
    "description",
)
PROTOCOLS = ("TCP", "UDP", "ICMP")


def as_port(value):
    """A port number, or None for anything the old form stored instead ('' or junk)."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def egress_rule_from_access_rule(rule, position):
    """An ingress-table row marked as egress, as a valid egress_rules entry.

    The old form saved rows the egress rule serializer refuses: a blank name,
    '' for cleared ports, ports on ICMP. Normalised here, or a pending order
    carrying such a row would fail its next edit or approval.
    """
    protocol = rule.get("protocol") if rule.get("protocol") in PROTOCOLS else ""
    port_min = as_port(rule.get("port_range_min"))
    port_max = as_port(rule.get("port_range_max"))
    if protocol == "ICMP" or (port_min is None and port_max is None):
        port_min = port_max = None
    else:
        port_min = port_min if port_min is not None else port_max
        port_max = (
            port_max if port_max is not None and port_max >= port_min else port_min
        )
    moved = {
        "name": str(rule.get("name") or "").strip() or f"Egress rule {position}",
        "destination_type": "cidr",
        "destination": str(rule.get("cidr") or "").strip() or "0.0.0.0/0",
        "protocol": protocol,
        "port_range_min": port_min,
        "port_range_max": port_max,
        "description": rule.get("description") or "",
    }
    if isinstance(rule.get("uuid"), str):
        moved["uuid"] = rule["uuid"][:64]
    return moved


def access_rule_from_egress_rule(rule):
    restored = {key: rule[key] for key in KEPT_RULE_KEYS if key in rule}
    restored["cidr"] = rule["destination"]
    restored["direction"] = "egress"
    return restored


def move_egress_rows(config):
    """Return ``config`` with egress rows moved to egress_rules, or None if it has none.

    The order form offered an Ingress/Egress choice on every row of the public
    and administrative access tables; outbound rules now have a list of their own.
    """
    if not isinstance(config, dict):
        return None
    result = dict(config)
    moved = []
    for key in INGRESS_RULE_KEYS:
        rules = config.get(key)
        if not isinstance(rules, list):
            continue
        egress = [
            rule
            for rule in rules
            if isinstance(rule, dict) and rule.get("direction") == "egress"
        ]
        if not egress:
            continue
        result[key] = [rule for rule in rules if not any(rule is e for e in egress)]
        first = len(moved) + 1
        moved.extend(
            egress_rule_from_access_rule(rule, first + i)
            for i, rule in enumerate(egress)
        )
    if not moved:
        return None
    existing = config.get(EGRESS_RULES_KEY)
    result[EGRESS_RULES_KEY] = [
        *(existing if isinstance(existing, list) else []),
        *moved,
    ]
    return result


def restore_egress_rows(config):
    """Reverse of move_egress_rows: network egress rules back to an access table.

    Every network rule goes back, including any added after the forward run.
    Domain rules have no place there and stay in egress_rules. The offerings'
    egress_rules_enabled is left as it is: which ones the forward run turned on
    is not recorded.
    """
    if not isinstance(config, dict):
        return None
    rules = config.get(EGRESS_RULES_KEY)
    if not isinstance(rules, list):
        return None
    networks = [
        rule
        for rule in rules
        if isinstance(rule, dict) and rule.get("destination_type") == "cidr"
    ]
    if not networks:
        return None
    result = dict(config)
    kept = [rule for rule in rules if not any(rule is n for n in networks)]
    if kept:
        result[EGRESS_RULES_KEY] = kept
    else:
        del result[EGRESS_RULES_KEY]
    existing = config.get(REVERSE_TARGET_KEY)
    result[REVERSE_TARGET_KEY] = [
        *(existing if isinstance(existing, list) else []),
        *(access_rule_from_egress_rule(rule) for rule in networks),
    ]
    return result


def k8s_option_names(options):
    if not isinstance(options, dict):
        return []
    fields = options.get("options")
    if not isinstance(fields, dict):
        return []
    return [
        name
        for name, option in fields.items()
        if isinstance(option, dict) and option.get("type") in K8S_CONFIG_FIELD_TYPES
    ]


def rewrite(row, field, names, change):
    """Apply ``change`` to the values of ``names`` in ``row.<field>``; True if any changed."""
    values = getattr(row, field)
    if not isinstance(values, dict):
        return False
    updated = dict(values)
    for name in names:
        new = change(values.get(name))
        if new is not None:
            updated[name] = new
    if updated == values:
        return False
    setattr(row, field, updated)
    row.save(update_fields=[field])
    return True


def enable_egress_rules(offering, names):
    """Let the offering accept the egress rules its orders now carry.

    Without this, a pending order whose egress rows moved would fail
    validation the next time it is edited or approved with changes.
    """
    options = dict(offering.options)
    fields = dict(options["options"])
    changed = False
    for name in names:
        option = dict(fields[name])
        default_configs = dict(option.get("default_configs") or {})
        if default_configs.get("egress_rules_enabled"):
            continue
        default_configs["egress_rules_enabled"] = True
        option["default_configs"] = default_configs
        fields[name] = option
        changed = True
    if changed:
        options["options"] = fields
        offering.options = options
        offering.save(update_fields=["options"])


def has_any_key(field, names):
    if not names:
        return Q(pk__in=[])
    return reduce(or_, (Q(**{f"{field}__has_key": name}) for name in names))


def apply(apps, change, on_moved=None):
    Offering = apps.get_model("marketplace", "Offering")
    Order = apps.get_model("marketplace", "Order")
    Resource = apps.get_model("marketplace", "Resource")
    for offering in Offering.objects.only("id", "options", "resource_options"):
        order_names = k8s_option_names(offering.options)
        resource_names = k8s_option_names(offering.resource_options)
        if not order_names and not resource_names:
            continue
        moved = False
        # Only rows that hold a cluster configuration, one at a time: the
        # tables are large, and most rows of a k8s offering may not.
        if order_names:
            orders = Order.objects.filter(
                has_any_key("attributes", order_names), offering_id=offering.id
            )
            for order in orders.only("id", "attributes").iterator():
                moved |= rewrite(order, "attributes", order_names, change)
        resources = Resource.objects.filter(
            has_any_key("attributes", order_names)
            | has_any_key("options", resource_names),
            offering_id=offering.id,
        )
        for resource in resources.only("id", "attributes", "options").iterator():
            if order_names:
                moved |= rewrite(resource, "attributes", order_names, change)
            if resource_names:
                rewrite(resource, "options", resource_names, change)
        if moved and on_moved:
            on_moved(offering, order_names)


def forwards(apps, schema_editor):
    apply(apps, move_egress_rows, on_moved=enable_egress_rules)


def backwards(apps, schema_editor):
    apply(apps, restore_egress_rows)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0329_offeringcomponent_limit_increase_only"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
