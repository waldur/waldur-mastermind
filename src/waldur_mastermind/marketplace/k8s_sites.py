"""Kubernetes clusters on OpenStack sites the provider fixed.

A k8s_cluster_config option with infrastructure_mode "provider_fixed" lists,
in fixed_openstack_offerings, the OpenStack tenant offerings a cluster is
deployed to, per topology: one for "1-datacenter", three for "3-datacenter".
The provider's automation provisions the tenants; the customer does not pick
them, and may only use flavors that every site has with the same vCPUs and
RAM. Node groups are defined once and repeated on every site, each with that
site's own flavor.
"""

import uuid

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_mastermind.common.serializers import (
    K8S_CLUSTER_CONFIG_FIELD_TYPE,
    K8S_INFRASTRUCTURE_PROVIDER_FIXED,
    K8S_TOPOLOGY_CUSTOMER_CHOICE,
    K8S_TOPOLOGY_DATACENTER_COUNTS,
)
from waldur_mastermind.marketplace import hooks, models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    OfferingStates,
)

FIXED_SITES_KEY = "fixed_openstack_offerings"
# A site must be a tenant offering the provider's organisation could order
# itself, and one that still takes orders: the provider's own, or one shared
# with it. Its name and flavors are shown to everyone who sees the cluster
# offering, so another organisation's private offering, or one its owner took
# down, must not be.
SITE_STATES = (OfferingStates.ACTIVE, OfferingStates.PAUSED)
NODE_GROUP_TYPES = ("worker", "storage")


def _site_offerings(customer):
    return (
        models.Offering.objects.all()
        .filter_for_customer(customer.uuid.hex)
        .filter(type=OPENSTACK_TENANT_OFFERING, state__in=SITE_STATES)
        .distinct()
    )


def is_provider_fixed(default_configs) -> bool:
    return (default_configs or {}).get(
        "infrastructure_mode"
    ) == K8S_INFRASTRUCTURE_PROVIDER_FIXED


def allowed_topologies(default_configs) -> list[str]:
    mode = (default_configs or {}).get("topology_mode") or K8S_TOPOLOGY_CUSTOMER_CHOICE
    if mode == K8S_TOPOLOGY_CUSTOMER_CHOICE:
        return list(K8S_TOPOLOGY_DATACENTER_COUNTS)
    return [mode]


def _hex(value) -> str:
    return str(value).replace("-", "").lower()


def _parse_uuids(values) -> list[str] | None:
    """The values as hex UUIDs; None if any is not a UUID."""
    if not isinstance(values, list):
        return None
    try:
        return [uuid.UUID(str(value)).hex for value in values]
    except ValueError:
        return None


def validate_fixed_sites_policy(default_configs) -> dict:
    """The shape of the provider's site lists, checked when the option is saved.

    Every topology the option allows needs its number of distinct sites; a
    list for a topology it does not allow is kept, so that changing the
    topology back does not lose it. Returns the lists with their UUIDs in hex
    form. Whose offerings they are is checked by check_offering_sites, which
    knows the offering.
    """
    sites = (default_configs or {}).get(FIXED_SITES_KEY) or {}
    if not isinstance(sites, dict):
        raise serializers.ValidationError(
            {FIXED_SITES_KEY: [_("Expected the site lists by topology.")]}
        )
    errors = []
    cleaned = {}
    allowed = allowed_topologies(default_configs)
    for topology in sites:
        if topology not in K8S_TOPOLOGY_DATACENTER_COUNTS:
            errors.append(_("Unknown topology %s.") % topology)
    for topology, expected in K8S_TOPOLOGY_DATACENTER_COUNTS.items():
        if topology not in sites and topology not in allowed:
            continue
        uuids = _parse_uuids(sites.get(topology) or [])
        if uuids is None:
            errors.append(_("The %s sites must be offering UUIDs.") % topology)
        elif len(uuids) != expected or len(set(uuids)) != expected:
            errors.append(
                _("The %(topology)s topology needs %(count)s different sites.")
                % {"topology": topology, "count": expected}
            )
        else:
            cleaned[topology] = uuids
    if errors:
        raise serializers.ValidationError({FIXED_SITES_KEY: errors})
    return cleaned


def check_offering_sites(options, customer):
    """The sites of an offering's Kubernetes options must be its provider's.

    Each must be an active or paused OpenStack tenant offering of the
    customer, or one shared with it. ``options`` is an offering's options or resource_options;
    errors are keyed as the option serializer keys them.
    """
    fields = (options or {}).get("options") or {}
    for name, option in fields.items():
        if not isinstance(option, dict):
            continue
        default_configs = option.get("default_configs") or {}
        if option.get("type") != K8S_CLUSTER_CONFIG_FIELD_TYPE or not (
            is_provider_fixed(default_configs)
        ):
            continue
        try:
            sites = validate_fixed_sites_policy(default_configs)
        except serializers.ValidationError as e:
            raise serializers.ValidationError(
                {"options": {name: {"default_configs": e.detail}}}
            )
        uuids = {uuid for site_uuids in sites.values() for uuid in site_uuids}
        found = {
            site.hex
            for site in _site_offerings(customer)
            .filter(uuid__in=uuids)
            .values_list("uuid", flat=True)
        }
        missing = sorted(uuids - found)
        if missing:
            message = _(
                "Not active OpenStack tenant offerings this provider may use: %s."
            ) % ", ".join(missing)
            raise serializers.ValidationError(
                {"options": {name: {"default_configs": {FIXED_SITES_KEY: [message]}}}}
            )


def get_sites(default_configs, topology, customer=None) -> list[models.Offering] | None:
    """The provider's sites for a topology, in order; None if any is gone.

    A site that is no longer an active or paused tenant offering, or, with
    ``customer``, no longer one that customer may use, counts as gone.
    """
    sites = (default_configs or {}).get(FIXED_SITES_KEY)
    uuids = _parse_uuids(sites.get(topology)) if isinstance(sites, dict) else None
    if not uuids:
        return None
    if customer is not None:
        queryset = _site_offerings(customer).filter(uuid__in=uuids)
    else:
        queryset = models.Offering.objects.filter(
            uuid__in=uuids, type=OPENSTACK_TENANT_OFFERING, state__in=SITE_STATES
        )
    by_uuid = {offering.uuid.hex: offering for offering in queryset}
    if len(by_uuid) != len(set(uuids)):
        return None
    return [by_uuid[uuid] for uuid in uuids]


def _site_flavors(site) -> dict:
    """The site's flavors by name; a name used twice is ambiguous and left out."""
    if not isinstance(site.scope, structure_models.ServiceSettings):
        return {}
    flavors = {}
    duplicates = set()
    for flavor in hooks.get_service_settings_flavors(site.scope):
        if flavor.name in flavors:
            duplicates.add(flavor.name)
        flavors[flavor.name] = flavor
    for name in duplicates:
        del flavors[name]
    return flavors


def common_flavors(sites) -> list[dict]:
    """Flavors every site has under one name with the same vCPUs and RAM.

    Each entry lists the flavor of every site, in the order of the sites.
    """
    per_site = [_site_flavors(site) for site in sites]
    if not per_site:
        return []
    names = set(per_site[0]).intersection(*per_site[1:])
    result = []
    for name in names:
        flavors = [site_flavors[name] for site_flavors in per_site]
        if len({(flavor.cores, flavor.ram) for flavor in flavors}) != 1:
            continue
        result.append(
            {
                "name": name,
                "cores": flavors[0].cores,
                "ram": flavors[0].ram,
                "disk": min(flavor.disk for flavor in flavors),
                "sites": [
                    {
                        "offering_uuid": _hex(site.uuid),
                        "flavor_uuid": _hex(flavor.uuid),
                    }
                    for site, flavor in zip(sites, flavors)
                ],
            }
        )
    return sorted(result, key=lambda f: (f["cores"], f["ram"], f["name"]))


def _cluster_topology(value, default_configs):
    topology = value.get("topology")
    if topology in K8S_TOPOLOGY_DATACENTER_COUNTS:
        return topology
    mode = (default_configs or {}).get("topology_mode")
    if mode in K8S_TOPOLOGY_DATACENTER_COUNTS:
        return mode
    for topology, count in K8S_TOPOLOGY_DATACENTER_COUNTS.items():
        if len(value["datacenters"]) == count:
            return topology
    return None


def _malformed(what):
    return serializers.ValidationError(_("Malformed cluster: %s.") % what)


def _parse_cluster(value):
    """The datacenters and their node groups, checked for shape.

    The value is the customer's JSON; anything not shaped as the order form
    sends it is refused here rather than failing later.
    """
    datacenters = value.get("datacenters")
    if not isinstance(datacenters, list):
        raise serializers.ValidationError(
            _("A cluster on the provider's sites must list its datacenters.")
        )
    for datacenter in datacenters:
        if not isinstance(datacenter, dict):
            raise _malformed(_("a datacenter is not an object"))
        if not isinstance(datacenter.get("openstack_infrastructure"), dict):
            raise _malformed(_("a datacenter has no OpenStack infrastructure"))
        groups = datacenter.get("node_groups") or []
        if not isinstance(groups, list):
            raise _malformed(_("node groups are not a list"))
        for group in groups:
            if not isinstance(group, dict):
                raise _malformed(_("a node group is not an object"))
            if group.get("type") not in NODE_GROUP_TYPES:
                raise _malformed(
                    _("a node group type must be one of: %s")
                    % ", ".join(NODE_GROUP_TYPES)
                )
            flavor = group.get("openstack_flavor")
            if not isinstance(flavor, dict) or not isinstance(flavor.get("name"), str):
                raise serializers.ValidationError(_("Every node group needs a flavor."))
    return datacenters


def _mirrored_shape(group):
    return (
        group.get("type"),
        group.get("node_count"),
        group.get("disk_config"),
        group["openstack_flavor"]["name"],
    )


def check_cluster_sites(value, option):
    """Check a cluster ordered on an offering whose provider fixed the sites.

    The sites must be the provider's for the topology, in order; the node
    groups the same on every site; and each flavor that site's own, named and
    sized as on the others. Worker flavors must meet the offering's minimums.
    """
    default_configs = option.get("default_configs") or {}
    if not is_provider_fixed(default_configs) or not isinstance(value, dict):
        return
    datacenters = _parse_cluster(value)

    topology = _cluster_topology(value, default_configs)
    sites = get_sites(default_configs, topology) if topology else None
    if not sites:
        raise serializers.ValidationError(
            _("This offering has no sites for the %s topology.") % topology
        )
    if len(datacenters) != len(sites):
        raise serializers.ValidationError(
            _("The %(topology)s topology deploys to %(count)s sites.")
            % {"topology": topology, "count": len(sites)}
        )

    for position, (datacenter, site) in enumerate(zip(datacenters, sites), 1):
        infrastructure = datacenter["openstack_infrastructure"]
        if _hex(infrastructure.get("uuid", "")) != _hex(site.uuid):
            raise serializers.ValidationError(
                _("%(name)s must use the provider's site %(site)s.")
                % {
                    "name": datacenter.get("name") or _("Datacenter %s") % position,
                    "site": site.name,
                }
            )

    groups_per_site = [
        datacenter.get("node_groups") or [] for datacenter in datacenters
    ]
    shapes = [
        [_mirrored_shape(group) for group in groups] for groups in groups_per_site
    ]
    if any(site_shapes != shapes[0] for site_shapes in shapes[1:]):
        raise serializers.ValidationError(
            _("Every site must have the same node groups.")
        )

    minimal_vcpus = default_configs.get("minimal_worker_vcpus")
    minimal_ram_gb = default_configs.get("minimal_worker_ram_gb")
    flavors_per_site = [_site_flavors(site) for site in sites]
    for site, groups, site_flavors in zip(sites, groups_per_site, flavors_per_site):
        for group in groups:
            ordered = group["openstack_flavor"]
            flavor = site_flavors.get(ordered["name"])
            if flavor is None or _hex(flavor.uuid) != _hex(ordered.get("uuid", "")):
                raise serializers.ValidationError(
                    _("Flavor %(flavor)s is not available on site %(site)s.")
                    % {"flavor": ordered["name"], "site": site.name}
                )
            if group["type"] == "worker" and (
                (minimal_vcpus and flavor.cores < minimal_vcpus)
                or (minimal_ram_gb and flavor.ram < minimal_ram_gb * 1024)
            ):
                raise serializers.ValidationError(
                    _("Flavor %s is below the minimum for worker nodes.") % flavor.name
                )

    # The same name on every site (checked above); the sizes must agree too.
    for group in groups_per_site[0]:
        name = group["openstack_flavor"]["name"]
        sizes = {
            (site_flavors[name].cores, site_flavors[name].ram)
            for site_flavors in flavors_per_site
        }
        if len(sizes) > 1:
            raise serializers.ValidationError(
                _("Flavor %s differs in size between the sites.") % name
            )
