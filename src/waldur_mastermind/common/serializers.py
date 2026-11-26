import ipaddress
import logging
import re

import regex
from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.logging.sentry import register_secret_value

logger = logging.getLogger(__name__)


class StringListSerializer(serializers.ListField):
    child = serializers.CharField()


class EmailListSerializer(serializers.ListField):
    child = serializers.EmailField()


class ConditionalCascadeField(serializers.DictField):
    """Field for conditional cascade selections that stores step-value mappings"""


K8S_TOPOLOGY_ONE_DATACENTER = "1-datacenter"
K8S_TOPOLOGY_THREE_DATACENTERS = "3-datacenter"
K8S_TOPOLOGY_CUSTOMER_CHOICE = "customer_choice"

K8S_TOPOLOGY_MODES = (
    (K8S_TOPOLOGY_ONE_DATACENTER, "Single site, 3 controllers"),
    (K8S_TOPOLOGY_THREE_DATACENTERS, "Three sites, 1 controller each"),
    (K8S_TOPOLOGY_CUSTOMER_CHOICE, "Customer chooses"),
)

K8S_TOPOLOGY_DATACENTER_COUNTS = {
    K8S_TOPOLOGY_ONE_DATACENTER: 1,
    K8S_TOPOLOGY_THREE_DATACENTERS: 3,
}


K8S_CLUSTER_CONFIG_FIELD_TYPE = "k8s_cluster_config"
K8S_EGRESS_RULES_FIELD_TYPE = "k8s_egress_rules"

K8S_RULE_PROTOCOLS = ("TCP", "UDP", "ICMP")
K8S_EGRESS_DESTINATION_CIDR = "cidr"
K8S_EGRESS_DESTINATION_DOMAIN = "domain"
K8S_EGRESS_DESTINATION_TYPES = (
    (K8S_EGRESS_DESTINATION_CIDR, "IPv4 or IPv6 network"),
    (K8S_EGRESS_DESTINATION_DOMAIN, "Domain name"),
)
# The ingress rule lists of a cluster configuration. Their egress rows moved to
# egress_rules (marketplace migration 0330), which is where they belong now.
K8S_INGRESS_RULE_KEYS = ("public_access_rules", "administrative_access_rules")
# The offering's egress policy, in the k8s option's default_configs; a
# k8s_egress_rules resource option carries a copy of it.
K8S_EGRESS_POLICY_KEYS = (
    "egress_rules_enabled",
    "egress_domain_rules_allowed",
    "egress_baseline_rules",
)

_DOMAIN_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# The last label must hold a letter, so an IP address is not taken for a name.
_DOMAIN_TOP_LABEL = r"(?!-)(?=[a-z0-9-]*[a-z])[a-z0-9-]{1,63}(?<!-)"
# A hostname of two or more labels, optionally led by a "*." wildcard.
_DOMAIN_PATTERN = re.compile(
    rf"^(\*\.)?(?:{_DOMAIN_LABEL}\.)+{_DOMAIN_TOP_LABEL}$", re.IGNORECASE
)
# An address with an explicit prefix length: no netmask form, no zone id.
_CIDR_SHAPE = re.compile(r"^[0-9a-f.:]+/\d{1,3}$", re.IGNORECASE)
MAX_DOMAIN_LENGTH = 253


def parse_network(value, strict=True):
    """The IPv4 or IPv6 network ``value`` writes, with an explicit prefix length.

    A bare address, a netmask or a zone id is refused. With ``strict`` the
    address must be the network's own, without host bits. Raises ValueError.
    """
    if not isinstance(value, str):
        raise ValueError(value)
    value = value.strip()
    if not _CIDR_SHAPE.match(value):
        raise ValueError(value)
    return ipaddress.ip_network(value, strict=strict)


def validate_egress_destination(destination_type, destination):
    """Raise a ValidationError unless ``destination`` is a valid value of its type."""
    if destination_type == K8S_EGRESS_DESTINATION_CIDR:
        try:
            # Host bits are tolerated: a rule may name a single host's network.
            parse_network(destination, strict=False)
        except ValueError:
            raise serializers.ValidationError(
                {
                    "destination": _(
                        "Enter an IPv4 or IPv6 network, such as 10.0.0.0/8 or 2001:db8::/32."
                    )
                }
            )
    elif len(destination) > MAX_DOMAIN_LENGTH or not _DOMAIN_PATTERN.match(destination):
        raise serializers.ValidationError(
            {
                "destination": _(
                    "Enter a domain name, such as registry.example.com, or a "
                    "wildcard covering its subdomains, such as *.example.com."
                )
            }
        )


class K8sEgressRuleSerializer(serializers.Serializer):
    """One outbound destination a Kubernetes cluster may reach.

    Protocol and ports are optional: a rule without them allows any traffic
    to the destination.
    """

    uuid = serializers.CharField(
        max_length=64,
        required=False,
        help_text=_("A client-side row id, kept as given. Ignored by the server."),
    )
    name = serializers.CharField(max_length=255)
    destination_type = serializers.ChoiceField(choices=K8S_EGRESS_DESTINATION_TYPES)
    destination = serializers.CharField(
        max_length=MAX_DOMAIN_LENGTH,
        help_text=_(
            "An IPv4 or IPv6 network for a cidr rule; a domain name, optionally "
            "led by a *. wildcard, for a domain rule."
        ),
    )
    protocol = serializers.ChoiceField(
        choices=K8S_RULE_PROTOCOLS,
        required=False,
        allow_null=True,
        allow_blank=True,
        help_text=_("Empty for any protocol."),
    )
    port_range_min = serializers.IntegerField(
        min_value=1,
        max_value=65535,
        required=False,
        allow_null=True,
        help_text=_("Empty for any port."),
    )
    port_range_max = serializers.IntegerField(
        min_value=1,
        max_value=65535,
        required=False,
        allow_null=True,
        help_text=_("Empty for a single port, port_range_min."),
    )
    description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )

    def validate(self, attrs):
        validate_egress_destination(attrs["destination_type"], attrs["destination"])
        port_min = attrs.get("port_range_min")
        port_max = attrs.get("port_range_max")
        if port_max is not None and port_min is None:
            raise serializers.ValidationError(
                {"port_range_min": _("Set the first port of the range too.")}
            )
        if port_min is not None and port_max is not None and port_min > port_max:
            raise serializers.ValidationError(
                {
                    "port_range_max": _(
                        "The last port must not be lower than the first one."
                    )
                }
            )
        if attrs.get("protocol") == "ICMP" and port_min is not None:
            raise serializers.ValidationError(
                {"port_range_min": _("ICMP has no ports.")}
            )
        if port_min is not None and port_max is None:
            attrs["port_range_max"] = port_min
        return attrs


class K8sEgressRulesField(serializers.ListField):
    """The outbound destinations a Kubernetes cluster may reach.

    An empty list leaves outbound traffic unrestricted. Any rule makes
    outbound traffic deny by default, with these destinations and the
    offering's baseline rules allowed. The offering's policy,
    the k8s option's default_configs, decides whether rules may be given at
    all and whether they may name domains.
    """

    child = K8sEgressRuleSerializer()

    def __init__(self, *args, **kwargs):
        default_configs = kwargs.pop("default_configs", None) or {}
        self.egress_rules_enabled = bool(default_configs.get("egress_rules_enabled"))
        self.egress_domain_rules_allowed = bool(
            default_configs.get("egress_domain_rules_allowed")
        )
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        rules = super().to_internal_value(data)
        if rules and not self.egress_rules_enabled:
            raise serializers.ValidationError(
                _("This offering does not accept egress rules.")
            )
        if not self.egress_domain_rules_allowed and any(
            rule["destination_type"] == K8S_EGRESS_DESTINATION_DOMAIN for rule in rules
        ):
            raise serializers.ValidationError(
                _("This offering accepts network (CIDR) egress destinations only.")
            )
        # Stored as cleaned: trimmed, ports as numbers, unknown keys dropped.
        return [dict(rule) for rule in rules]


def validate_k8s_ingress_rules(value):
    """Check the shape of a cluster configuration's ingress rule lists.

    Only the shape: these lists were stored unchecked before, and orders
    placed then must keep validating. Outbound rules go to egress_rules.
    """
    for key in K8S_INGRESS_RULE_KEYS:
        rules = value.get(key)
        if rules is None:
            continue
        if not isinstance(rules, list) or not all(
            isinstance(rule, dict) for rule in rules
        ):
            raise serializers.ValidationError(
                {key: _("Expected a list of rule objects.")}
            )
        if any(rule.get("direction") == "egress" for rule in rules):
            raise serializers.ValidationError(
                {key: _("Outbound rules belong in egress_rules.")}
            )


class K8sConfigField(serializers.DictField):
    """
    Kubernetes cluster configuration chosen in the order form.

    The offering's topology_mode decides which topology the value may carry;
    when the option does not set it, the customer chooses. Values that lack
    the topology or datacenters keys are left alone, so orders placed before
    these checks existed still validate.

    The egress_rules list is checked against the offering's egress policy
    (see K8sEgressRulesField).
    """

    def __init__(self, *args, **kwargs):
        self.default_configs = kwargs.pop("default_configs", None) or {}
        self.topology_mode = (
            self.default_configs.get("topology_mode") or K8S_TOPOLOGY_CUSTOMER_CHOICE
        )
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        validate_k8s_ingress_rules(value)
        egress_rules = value.get("egress_rules")
        if egress_rules is not None:
            try:
                value["egress_rules"] = K8sEgressRulesField(
                    default_configs=self.default_configs
                ).run_validation(egress_rules)
            except serializers.ValidationError as e:
                raise serializers.ValidationError({"egress_rules": e.detail})
        self._validate_topology(value)
        return value

    def _validate_topology(self, value):
        topology = value.get("topology")

        if topology is not None:
            if (
                not isinstance(topology, str)
                or topology not in K8S_TOPOLOGY_DATACENTER_COUNTS
            ):
                raise serializers.ValidationError(
                    "Cluster topology must be one of: "
                    + ", ".join(K8S_TOPOLOGY_DATACENTER_COUNTS)
                    + "."
                )
            if self.topology_mode not in (K8S_TOPOLOGY_CUSTOMER_CHOICE, topology):
                raise serializers.ValidationError(
                    f'This offering does not allow the "{topology}" cluster topology.'
                )
        elif self.topology_mode != K8S_TOPOLOGY_CUSTOMER_CHOICE:
            topology = self.topology_mode

        datacenters = value.get("datacenters")
        if not isinstance(datacenters, list):
            return

        if topology is None:
            # The customer's pick was not recorded: any supported site count.
            if len(datacenters) not in K8S_TOPOLOGY_DATACENTER_COUNTS.values():
                raise serializers.ValidationError(
                    "A cluster needs 1 or 3 datacenters, "
                    f"but {len(datacenters)} were given."
                )
            return

        expected = K8S_TOPOLOGY_DATACENTER_COUNTS[topology]
        if len(datacenters) != expected:
            raise serializers.ValidationError(
                f'The "{topology}" cluster topology requires {expected} '
                f"datacenter(s), but {len(datacenters)} were given."
            )


class StorageFolderManagerField(serializers.Serializer):
    storage_data_type = serializers.CharField(required=True)
    permissions = serializers.CharField(required=True)
    hard_quota_space = serializers.FloatField(
        required=False, validators=[MinValueValidator(0.01)]
    )
    soft_quota_inodes = serializers.IntegerField(
        required=False, validators=[MinValueValidator(1)]
    )
    hard_quota_inodes = serializers.IntegerField(
        required=False, validators=[MinValueValidator(1)]
    )


# --- Network options: cidr and ipsec_vpn_config ------------------------------

CIDR_FIELD_TYPE = "cidr"
IPSEC_VPN_CONFIG_FIELD_TYPE = "ipsec_vpn_config"
NETWORK_FIELD_TYPES = (CIDR_FIELD_TYPE, IPSEC_VPN_CONFIG_FIELD_TYPE)

IP_VERSION_IPV4 = "ipv4"
IP_VERSION_IPV6 = "ipv6"
IP_VERSION_ANY = "any"
IP_VERSIONS = (
    (IP_VERSION_IPV4, "IPv4 only"),
    (IP_VERSION_IPV6, "IPv6 only"),
    (IP_VERSION_ANY, "IPv4 or IPv6"),
)
_IP_VERSION_NUMBERS = {IP_VERSION_IPV4: 4, IP_VERSION_IPV6: 6}
_MAX_PREFIX_LENGTH = {4: 32, 6: 128}
# A list longer than this is refused even without max_items: entries are
# compared with each other for overlaps.
MAX_CIDR_ITEMS = 256

# The keys of a cidr option's default_configs.
CIDR_POLICY_KEYS = (
    "ip_version",
    "prefix_length_min",
    "prefix_length_max",
    "allowed_ranges",
    "excluded_ranges",
    "multiple",
    "max_items",
)

CIDR_FORMAT_ERROR = _("Enter a network in CIDR notation, such as 10.0.0.0/16.")


def _overlaps(first, second):
    return first.version == second.version and first.overlaps(second)


def _subnet_of(network, other):
    return network.version == other.version and network.subnet_of(other)


def clean_network(value):
    """The network ``value`` writes, without host bits, or a ValidationError.

    A network with host bits set is refused with the network it lies in, so
    the user sees what they probably meant.
    """
    try:
        return parse_network(value)
    except ValueError:
        pass
    try:
        loose = parse_network(value, strict=False)
    except ValueError:
        raise serializers.ValidationError(CIDR_FORMAT_ERROR)
    raise serializers.ValidationError(
        _("%(value)s has host bits set. Did you mean %(network)s?")
        % {"value": value.strip(), "network": loose}
    )


def _policy_networks(ranges):
    """Networks of a stored policy; one written around its check is skipped."""
    networks = []
    for item in ranges or []:
        try:
            networks.append(parse_network(item, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid network %r in an option policy.", item)
    return networks


class CidrPolicy:
    """The rules a cidr option's value, or an IPsec option's remote subnets, obey.

    Built from the option's default_configs; every key is optional. An empty
    policy accepts any IPv4 or IPv6 network.
    """

    def __init__(self, configs=None):
        configs = configs or {}
        self.ip_version = configs.get("ip_version") or IP_VERSION_ANY
        self.prefix_length_min = configs.get("prefix_length_min")
        self.prefix_length_max = configs.get("prefix_length_max")
        self.allowed_ranges = _policy_networks(configs.get("allowed_ranges"))
        self.excluded_ranges = _policy_networks(configs.get("excluded_ranges"))
        self.multiple = bool(configs.get("multiple"))
        self.max_items = configs.get("max_items")

    def clean_network(self, value):
        """One network checked against the policy, as an ipaddress network."""
        if not isinstance(value, str):
            raise serializers.ValidationError(CIDR_FORMAT_ERROR)
        network = clean_network(value)
        version = _IP_VERSION_NUMBERS.get(self.ip_version)
        if version and network.version != version:
            raise serializers.ValidationError(
                _("Enter an IPv%(version)s network.") % {"version": version}
            )
        if (
            self.prefix_length_min is not None
            and network.prefixlen < self.prefix_length_min
        ):
            raise serializers.ValidationError(
                _("The network is too large: the prefix length must be at least /%s.")
                % self.prefix_length_min
            )
        if (
            self.prefix_length_max is not None
            and network.prefixlen > self.prefix_length_max
        ):
            raise serializers.ValidationError(
                _("The network is too small: the prefix length must be at most /%s.")
                % self.prefix_length_max
            )
        if self.allowed_ranges and not any(
            _subnet_of(network, allowed) for allowed in self.allowed_ranges
        ):
            raise serializers.ValidationError(
                _("%(network)s is not inside the allowed ranges: %(ranges)s.")
                % {
                    "network": network,
                    "ranges": ", ".join(str(item) for item in self.allowed_ranges),
                }
            )
        for excluded in self.excluded_ranges:
            if _overlaps(network, excluded):
                raise serializers.ValidationError(
                    _("%(network)s overlaps the excluded range %(range)s.")
                    % {"network": network, "range": excluded}
                )
        return network

    def clean_networks(self, values, allow_empty=True):
        """A list of networks: each checked, none overlapping another."""
        if not isinstance(values, list):
            raise serializers.ValidationError(_("Enter a list of networks."))
        if not values and not allow_empty:
            raise serializers.ValidationError(_("Enter at least one network."))
        limit = min(self.max_items or MAX_CIDR_ITEMS, MAX_CIDR_ITEMS)
        if len(values) > limit:
            raise serializers.ValidationError(_("Enter at most %s networks.") % limit)
        networks = []
        errors = {}
        for index, value in enumerate(values):
            try:
                networks.append(self.clean_network(value))
            except serializers.ValidationError as e:
                errors[index] = e.detail
        if errors:
            raise serializers.ValidationError(errors)
        for index, network in enumerate(networks):
            for other in networks[:index]:
                if _overlaps(network, other):
                    raise serializers.ValidationError(
                        _("%(network)s overlaps %(other)s.")
                        % {"network": network, "other": other}
                    )
        return [str(network) for network in networks]

    def clean_value(self, value, required=False):
        """A cidr option's value: one network, or a list when ``multiple``.

        With ``multiple``, a single network sent as a string is taken as a
        list of one.
        """
        if self.multiple:
            if isinstance(value, str):
                value = [value]
            return self.clean_networks(value, allow_empty=not required)
        if isinstance(value, list):
            raise serializers.ValidationError(_("Enter a single network."))
        return str(self.clean_network(value))


def validate_cidr_policy(attrs, ip_version_key="ip_version"):
    """Check a cidr policy as an offering saves it, and normalise its ranges.

    Returns the policy; raises a ValidationError keyed by the policy's keys.
    """
    errors = {}
    version = _IP_VERSION_NUMBERS.get(attrs.get(ip_version_key) or IP_VERSION_ANY)
    highest = _MAX_PREFIX_LENGTH[version] if version else _MAX_PREFIX_LENGTH[6]
    for key in ("prefix_length_min", "prefix_length_max"):
        length = attrs.get(key)
        if length is not None and not 0 <= length <= highest:
            errors[key] = _("Enter a prefix length from 0 to %s.") % highest
    low = attrs.get("prefix_length_min")
    high = attrs.get("prefix_length_max")
    if not errors and low is not None and high is not None and low > high:
        errors["prefix_length_max"] = _(
            "The largest prefix length must not be lower than the smallest."
        )
    for key in ("allowed_ranges", "excluded_ranges"):
        if key not in attrs:
            continue
        ranges = []
        for item in attrs[key]:
            try:
                network = clean_network(item)
            except serializers.ValidationError as e:
                errors[key] = e.detail
                break
            if version and network.version != version:
                errors[key] = _("%(network)s is not an IPv%(version)s network.") % {
                    "network": network,
                    "version": version,
                }
                break
            ranges.append(str(network))
        else:
            attrs[key] = ranges
    max_items = attrs.get("max_items")
    if max_items is not None and max_items > MAX_CIDR_ITEMS:
        errors["max_items"] = _("Enter at most %s.") % MAX_CIDR_ITEMS
    if errors:
        raise serializers.ValidationError(errors)
    return attrs


class CidrPolicySerializer(serializers.Serializer):
    """The rules of networks entered as CIDR (a cidr option's default_configs)."""

    ip_version = serializers.ChoiceField(
        choices=IP_VERSIONS,
        required=False,
        help_text=_("Which IP version the network may be. Any when omitted."),
    )
    prefix_length_min = serializers.IntegerField(
        min_value=0,
        max_value=128,
        required=False,
        help_text=_("The smallest prefix length allowed, that is the largest network."),
    )
    prefix_length_max = serializers.IntegerField(
        min_value=0,
        max_value=128,
        required=False,
        help_text=_("The largest prefix length allowed, that is the smallest network."),
    )
    allowed_ranges = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_(
            "Networks the value must lie inside one of, such as the private "
            "ranges 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 and fc00::/7. "
            "Any network when empty."
        ),
    )
    excluded_ranges = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text=_("Networks the value must not overlap."),
    )
    max_items = serializers.IntegerField(
        min_value=1,
        required=False,
        help_text=_("For a list: the most networks it may hold."),
    )

    def validate(self, attrs):
        return validate_cidr_policy(attrs)


class CidrOptionPolicySerializer(CidrPolicySerializer):
    multiple = serializers.BooleanField(
        required=False,
        help_text=_("Whether the value is a list of networks. Off when omitted."),
    )


class CidrField(serializers.Field):
    """A network in CIDR notation, or a list of them, under the option's policy.

    Stored normalised: without surrounding whitespace, IPv6 compressed and in
    lower case.
    """

    def __init__(self, *args, **kwargs):
        self.policy = CidrPolicy(kwargs.pop("default_configs", None))
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        return self.policy.clean_value(data, required=self.required)

    def to_representation(self, value):
        return value


def cidr_value_networks(value):
    """The networks of a stored cidr value, a string or a list; bad ones skipped."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return _policy_networks(value)


IPSEC_ENCRYPTION_ALGORITHMS = (
    ("aes128", "AES-128"),
    ("aes192", "AES-192"),
    ("aes256", "AES-256"),
    ("aes128gcm16", "AES-128-GCM"),
    ("aes256gcm16", "AES-256-GCM"),
    ("chacha20poly1305", "ChaCha20-Poly1305"),
)
IPSEC_INTEGRITY_ALGORITHMS = (
    ("sha1", "SHA-1"),
    ("sha256", "SHA-256"),
    ("sha384", "SHA-384"),
    ("sha512", "SHA-512"),
)
IPSEC_DH_GROUPS = (
    (2, "MODP 1024 (group 2)"),
    (5, "MODP 1536 (group 5)"),
    (14, "MODP 2048 (group 14)"),
    (15, "MODP 3072 (group 15)"),
    (16, "MODP 4096 (group 16)"),
    (19, "ECP 256 (group 19)"),
    (20, "ECP 384 (group 20)"),
    (21, "ECP 521 (group 21)"),
    (31, "Curve25519 (group 31)"),
)
# Left out of the allowed lists unless a provider names them.
IPSEC_WEAK_INTEGRITY_ALGORITHMS = ("sha1",)
IPSEC_WEAK_DH_GROUPS = (2, 5)

IKE_VERSION_IKEV1 = "ikev1"
IKE_VERSION_IKEV2 = "ikev2"
IKE_VERSIONS = ((IKE_VERSION_IKEV2, "IKEv2"), (IKE_VERSION_IKEV1, "IKEv1"))
IPSEC_DPD_ACTIONS = (
    ("restart", "Restart the tunnel"),
    ("clear", "Close the tunnel"),
    ("hold", "Hold traffic until the peer is back"),
)

# Built-in phase policies: what applies where the provider sets nothing.
IPSEC_PHASE_DEFAULTS = {
    "phase1": {
        "group_key": "dh_group",
        "groups_key": "dh_groups",
        "lifetime_min": 3600,
        "lifetime_max": 86400,
        "defaults": {
            "encryption": "aes256",
            "integrity": "sha256",
            "dh_group": 14,
            "lifetime": 28800,
        },
    },
    "phase2": {
        "group_key": "pfs_group",
        "groups_key": "pfs_groups",
        "lifetime_min": 900,
        "lifetime_max": 28800,
        "defaults": {
            "encryption": "aes256",
            "integrity": "sha256",
            "pfs_group": 14,
            "lifetime": 3600,
        },
    },
}

# The keys of an ipsec_vpn_config option's default_configs.
IPSEC_POLICY_KEYS = (
    "ikev1_allowed",
    "allow_private_peer",
    "provider_gateway_ip",
    "provider_local_subnets",
    "remote_subnets",
    "no_overlap_with",
    "phase1",
    "phase2",
)

# Networks a VPN peer may not be in unless the offering allows private peers,
# besides what ipaddress calls loopback, link-local, multicast, reserved or
# unspecified. Documentation ranges (RFC 5737, RFC 3849) are accepted.
_NON_PUBLIC_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


def is_public_address(address):
    """Whether a VPN peer at ``address`` can be reached over the internet."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return not any(
        address.version == network.version and address in network
        for network in _NON_PUBLIC_NETWORKS
    )


_IKE_ID_EMAIL = re.compile(r"^[^@\s]{1,64}@(?P<domain>[^@\s]+)$")


def is_ike_identity(value):
    """Whether ``value`` is an IKE identity: an IP address, an FQDN or user@FQDN."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    match = _IKE_ID_EMAIL.match(value)
    domain = match.group("domain") if match else value
    return (
        len(domain) <= MAX_DOMAIN_LENGTH
        and not domain.startswith("*.")
        and bool(_DOMAIN_PATTERN.match(domain))
    )


class IpsecPhase1Serializer(serializers.Serializer):
    """IKE (phase 1) proposal of a tunnel."""

    encryption = serializers.ChoiceField(
        choices=IPSEC_ENCRYPTION_ALGORITHMS, required=False
    )
    integrity = serializers.ChoiceField(
        choices=IPSEC_INTEGRITY_ALGORITHMS, required=False
    )
    dh_group = serializers.ChoiceField(choices=IPSEC_DH_GROUPS, required=False)
    lifetime = serializers.IntegerField(
        min_value=1, required=False, help_text=_("In seconds.")
    )


class IpsecPhase2Serializer(serializers.Serializer):
    """ESP (phase 2) proposal of a tunnel."""

    encryption = serializers.ChoiceField(
        choices=IPSEC_ENCRYPTION_ALGORITHMS, required=False
    )
    integrity = serializers.ChoiceField(
        choices=IPSEC_INTEGRITY_ALGORITHMS, required=False
    )
    pfs_group = serializers.ChoiceField(
        choices=IPSEC_DH_GROUPS,
        required=False,
        allow_null=True,
        help_text=_("Null for no perfect forward secrecy."),
    )
    lifetime = serializers.IntegerField(
        min_value=1, required=False, help_text=_("In seconds.")
    )


class IpsecPhase1PolicySerializer(serializers.Serializer):
    encryption = serializers.ListField(
        child=serializers.ChoiceField(choices=IPSEC_ENCRYPTION_ALGORITHMS),
        required=False,
        allow_empty=False,
        help_text=_("Allowed. Every algorithm when omitted."),
    )
    integrity = serializers.ListField(
        child=serializers.ChoiceField(choices=IPSEC_INTEGRITY_ALGORITHMS),
        required=False,
        allow_empty=False,
        help_text=_("Allowed. Every algorithm but sha1 when omitted."),
    )
    dh_groups = serializers.ListField(
        child=serializers.ChoiceField(choices=IPSEC_DH_GROUPS),
        required=False,
        allow_empty=False,
        help_text=_("Allowed. Every group but 2 and 5 when omitted."),
    )
    lifetime_min = serializers.IntegerField(min_value=1, required=False)
    lifetime_max = serializers.IntegerField(min_value=1, required=False)
    defaults = IpsecPhase1Serializer(required=False)


class IpsecPhase2PolicySerializer(serializers.Serializer):
    encryption = serializers.ListField(
        child=serializers.ChoiceField(choices=IPSEC_ENCRYPTION_ALGORITHMS),
        required=False,
        allow_empty=False,
        help_text=_("Allowed. Every algorithm when omitted."),
    )
    integrity = serializers.ListField(
        child=serializers.ChoiceField(choices=IPSEC_INTEGRITY_ALGORITHMS),
        required=False,
        allow_empty=False,
        help_text=_("Allowed. Every algorithm but sha1 when omitted."),
    )
    pfs_groups = serializers.ListField(
        child=serializers.ChoiceField(choices=IPSEC_DH_GROUPS, allow_null=True),
        required=False,
        allow_empty=False,
        help_text=_(
            "Allowed. Every group but 2 and 5 when omitted. Null allows "
            "no perfect forward secrecy."
        ),
    )
    lifetime_min = serializers.IntegerField(min_value=1, required=False)
    lifetime_max = serializers.IntegerField(min_value=1, required=False)
    defaults = IpsecPhase2Serializer(required=False)


class IpsecProviderEndSerializer(serializers.Serializer):
    """The provider's end of a tunnel."""

    gateway_ip = serializers.IPAddressField(required=False, allow_null=True)
    local_subnets = serializers.ListField(child=serializers.CharField(), required=False)

    def validate_local_subnets(self, value):
        return CidrPolicy({"multiple": True}).clean_networks(value)


class IpsecDpdSerializer(serializers.Serializer):
    """Dead peer detection."""

    action = serializers.ChoiceField(choices=IPSEC_DPD_ACTIONS)
    interval = serializers.IntegerField(min_value=1, help_text=_("In seconds."))
    timeout = serializers.IntegerField(min_value=1, help_text=_("In seconds."))

    def validate(self, attrs):
        if attrs["timeout"] < attrs["interval"]:
            raise serializers.ValidationError(
                {"timeout": _("The timeout must not be shorter than the interval.")}
            )
        return attrs


class IpsecVpnConfigSerializer(serializers.Serializer):
    """The shape of an ipsec_vpn_config value as a customer sends it."""

    peer_gateway_ip = serializers.CharField(
        max_length=64, help_text=_("Public IP address of the customer's gateway.")
    )
    peer_id = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text=_("IKE identity of the gateway: an IP address, FQDN or user@FQDN."),
    )
    remote_subnets = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        help_text=_("Networks behind the customer's gateway."),
    )
    ike_version = serializers.ChoiceField(choices=IKE_VERSIONS, required=False)
    phase1 = IpsecPhase1Serializer(required=False)
    phase2 = IpsecPhase2Serializer(required=False)
    dpd = IpsecDpdSerializer(required=False, allow_null=True)
    provider = IpsecProviderEndSerializer(
        required=False,
        help_text=_("Set by the server; only the provider can change it."),
    )


def _phase_policy(configs, phase):
    """A phase's allowed lists, lifetime limits and defaults, filled in."""
    builtin = IPSEC_PHASE_DEFAULTS[phase]
    configs = configs or {}
    group_key = builtin["group_key"]
    allowed = {
        "encryption": list(
            configs.get("encryption")
            or [key for key, _label in IPSEC_ENCRYPTION_ALGORITHMS]
        ),
        "integrity": list(
            configs.get("integrity")
            or [
                key
                for key, _label in IPSEC_INTEGRITY_ALGORITHMS
                if key not in IPSEC_WEAK_INTEGRITY_ALGORITHMS
            ]
        ),
        group_key: list(
            configs.get(builtin["groups_key"])
            or [
                key
                for key, _label in IPSEC_DH_GROUPS
                if key not in IPSEC_WEAK_DH_GROUPS
            ]
        ),
    }
    lifetime_min = configs.get("lifetime_min") or builtin["lifetime_min"]
    lifetime_max = configs.get("lifetime_max") or builtin["lifetime_max"]
    given = configs.get("defaults") or {}
    defaults = {}
    for key, choices in allowed.items():
        if key in given:
            defaults[key] = given[key]
        elif builtin["defaults"][key] in choices:
            defaults[key] = builtin["defaults"][key]
        else:
            defaults[key] = choices[0]
    lifetime = given.get("lifetime", builtin["defaults"]["lifetime"])
    defaults["lifetime"] = min(max(lifetime, lifetime_min), lifetime_max)
    return {
        "allowed": allowed,
        "lifetime_min": lifetime_min,
        "lifetime_max": lifetime_max,
        "defaults": defaults,
    }


class IpsecPolicy:
    """The rules of an ipsec_vpn_config option, from its default_configs.

    Every key is optional; what is missing takes the built-in defaults.
    """

    def __init__(self, configs=None):
        configs = configs or {}
        self.ikev1_allowed = bool(configs.get("ikev1_allowed"))
        self.allow_private_peer = bool(configs.get("allow_private_peer"))
        self.provider_gateway_ip = configs.get("provider_gateway_ip") or None
        self.provider_local_subnets = list(configs.get("provider_local_subnets") or [])
        self.remote_subnets = CidrPolicy(
            {**(configs.get("remote_subnets") or {}), "multiple": True}
        )
        self.no_overlap_with = list(configs.get("no_overlap_with") or [])
        self.phases = {
            phase: _phase_policy(configs.get(phase), phase)
            for phase in IPSEC_PHASE_DEFAULTS
        }

    def default_provider_end(self):
        return {
            "gateway_ip": self.provider_gateway_ip,
            "local_subnets": list(self.provider_local_subnets),
        }

    def clean_phase(self, phase, given):
        """A phase of the value, filled from the defaults, or a dict of errors."""
        policy = self.phases[phase]
        value = {**policy["defaults"], **(given or {})}
        errors = {}
        for key, allowed in policy["allowed"].items():
            if value[key] not in allowed:
                errors[key] = _(
                    "%(value)s is not allowed. Choose one of: %(allowed)s."
                ) % {
                    "value": "none" if value[key] is None else value[key],
                    "allowed": ", ".join(
                        "none" if item is None else str(item) for item in allowed
                    ),
                }
        if not policy["lifetime_min"] <= value["lifetime"] <= policy["lifetime_max"]:
            errors["lifetime"] = _(
                "Enter a lifetime from %(min)s to %(max)s seconds."
            ) % {"min": policy["lifetime_min"], "max": policy["lifetime_max"]}
        return value, errors


def validate_ipsec_policy(attrs):
    """Check an ipsec_vpn_config policy as an offering saves it.

    References to other options (no_overlap_with) are checked by the offering,
    which holds both option lists.
    """
    errors = {}
    if attrs.get("provider_gateway_ip"):
        try:
            attrs["provider_gateway_ip"] = str(
                ipaddress.ip_address(attrs["provider_gateway_ip"].strip())
            )
        except ValueError:
            errors["provider_gateway_ip"] = _("Enter a valid IPv4 or IPv6 address.")
    if "provider_local_subnets" in attrs:
        try:
            attrs["provider_local_subnets"] = CidrPolicy(
                {"multiple": True}
            ).clean_networks(attrs["provider_local_subnets"])
        except serializers.ValidationError as e:
            errors["provider_local_subnets"] = e.detail
    for phase, builtin in IPSEC_PHASE_DEFAULTS.items():
        configs = attrs.get(phase)
        if not configs:
            continue
        phase_errors = {}
        low = configs.get("lifetime_min") or builtin["lifetime_min"]
        high = configs.get("lifetime_max") or builtin["lifetime_max"]
        if low > high:
            phase_errors["lifetime_max"] = _(
                "The longest lifetime must not be shorter than the shortest."
            )
        defaults = configs.get("defaults") or {}
        policy = _phase_policy(configs, phase)
        default_errors = {
            key: _("The default must be one of the allowed values.")
            for key, allowed in policy["allowed"].items()
            if key in defaults and defaults[key] not in allowed
        }
        if "lifetime" in defaults and not low <= defaults["lifetime"] <= high:
            default_errors["lifetime"] = _(
                "The default lifetime must be within the lifetime limits."
            )
        if default_errors:
            phase_errors["defaults"] = default_errors
        if phase_errors:
            errors[phase] = phase_errors
    if errors:
        raise serializers.ValidationError(errors)
    return attrs


class IpsecVpnConfigField(serializers.Field):
    """A site-to-site IPsec VPN configuration, checked against the option's policy.

    The cleaned value is stored: phase settings the customer left out are
    filled from the policy, and the provider's end (``provider``) is set by the
    server, from the value already stored or the policy's defaults. Only the
    provider, with ``trust_provider_end``, changes it.

    A pre-shared key is not part of it: an offering asks for one with a
    secret option next to it.
    """

    def __init__(self, *args, **kwargs):
        self.policy = IpsecPolicy(kwargs.pop("default_configs", None))
        self.stored_value = kwargs.pop("stored_value", None)
        self.trust_provider_end = kwargs.pop("trust_provider_end", False)
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError(
                _("Expected an object with the VPN settings.")
            )
        serializer = IpsecVpnConfigSerializer(data=data)
        if not serializer.is_valid():
            raise serializers.ValidationError(serializer.errors)
        submitted = serializer.validated_data
        policy = self.policy
        errors = {}

        peer = None
        try:
            peer = ipaddress.ip_address(submitted["peer_gateway_ip"].strip())
        except ValueError:
            errors["peer_gateway_ip"] = _(
                "Enter the IP address of your VPN gateway, such as 198.51.100.10."
            )
        if (
            peer is not None
            and not policy.allow_private_peer
            and not is_public_address(peer)
        ):
            errors["peer_gateway_ip"] = (
                _(
                    "%s is not a public address. Enter the public IP address of "
                    "your VPN gateway."
                )
                % peer
            )

        peer_id = (submitted.get("peer_id") or "").strip()
        if peer_id and not is_ike_identity(peer_id):
            errors["peer_id"] = _(
                "Enter an IP address, a domain name or an identity such as "
                "vpn@example.com."
            )

        ike_version = submitted.get("ike_version") or IKE_VERSION_IKEV2
        if ike_version == IKE_VERSION_IKEV1 and not policy.ikev1_allowed:
            errors["ike_version"] = _("This offering accepts IKEv2 only.")

        phases = {}
        for phase in IPSEC_PHASE_DEFAULTS:
            phases[phase], phase_errors = policy.clean_phase(
                phase, submitted.get(phase)
            )
            if phase_errors:
                errors[phase] = phase_errors

        remote_subnets = []
        try:
            remote_subnets = policy.remote_subnets.clean_networks(
                submitted["remote_subnets"], allow_empty=False
            )
        except serializers.ValidationError as e:
            errors["remote_subnets"] = e.detail

        provider = self._provider_end(submitted)
        if "remote_subnets" not in errors:
            self._check_provider_overlap(remote_subnets, provider, submitted, errors)

        if errors:
            raise serializers.ValidationError(errors)
        dpd = submitted.get("dpd")
        return {
            "peer_gateway_ip": str(peer),
            "peer_id": peer_id,
            "remote_subnets": remote_subnets,
            "ike_version": ike_version,
            "phase1": phases["phase1"],
            "phase2": phases["phase2"],
            "dpd": dict(dpd) if dpd else None,
            "provider": provider,
        }

    def _check_provider_overlap(self, remote_subnets, provider, submitted, errors):
        """Refuse remote subnets overlapping the provider end's local subnets.

        Both ends of a site-to-site tunnel route the other's networks through
        it, so a network on both sides breaks routing. The provider end is the
        one the value will be stored with. When the provider changes it
        (update_options_direct), the error is reported on its subnets.
        """
        local_subnets = _policy_networks(provider.get("local_subnets"))
        provider_changed = self.trust_provider_end and "local_subnets" in (
            submitted.get("provider") or {}
        )
        for subnet in _policy_networks(remote_subnets):
            for local in local_subnets:
                if not _overlaps(subnet, local):
                    continue
                if provider_changed:
                    errors["provider"] = {
                        "local_subnets": [
                            _(
                                "%(provider_subnet)s overlaps the remote subnet "
                                "%(subnet)s."
                            )
                            % {"provider_subnet": local, "subnet": subnet}
                        ]
                    }
                else:
                    errors["remote_subnets"] = [
                        _(
                            "%(subnet)s overlaps the provider's subnet %(provider_subnet)s."
                        )
                        % {"subnet": subnet, "provider_subnet": local}
                    ]
                return

    def _provider_end(self, submitted):
        if self.trust_provider_end and "provider" in submitted:
            stored = self._stored_provider_end() or self.policy.default_provider_end()
            return {**stored, **submitted["provider"]}
        return self._stored_provider_end() or self.policy.default_provider_end()

    def _stored_provider_end(self):
        stored = self.stored_value
        if isinstance(stored, dict) and isinstance(stored.get("provider"), dict):
            return dict(stored["provider"])
        return None

    def to_representation(self, value):
        return value


SECRET_FIELD_TYPE = "secret"

SECRET_SOURCE_CUSTOMER = "customer"
SECRET_SOURCE_PROVIDER = "provider"
SECRET_SOURCES = (
    (SECRET_SOURCE_CUSTOMER, "The customer enters it"),
    (SECRET_SOURCE_PROVIDER, "The provider or its site agent sets it"),
)
SECRET_DEFAULT_MIN_LENGTH = 1
SECRET_DEFAULT_MAX_LENGTH = 4096
SECRET_MAX_LENGTH_CAP = 65536

# The keys of a secret option's default_configs.
SECRET_POLICY_KEYS = (
    "secret_source",
    "customer_can_reveal",
    "min_length",
    "max_length",
    "multiline",
)

# Control characters a single-line secret may not hold; with multiline, a
# line break (CR, LF) and a tab are allowed too.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_MULTILINE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SecretPolicy:
    """The rules of a secret option, from its default_configs.

    Every key is optional. A customer-sourced secret is shown to customers
    only as set, unless customer_can_reveal says otherwise; a provider-sourced
    one may be revealed by default.
    """

    def __init__(self, configs=None):
        configs = configs or {}
        self.source = configs.get("secret_source") or SECRET_SOURCE_CUSTOMER
        reveal = configs.get("customer_can_reveal")
        self.customer_can_reveal = (
            bool(reveal)
            if reveal is not None
            else self.source == SECRET_SOURCE_PROVIDER
        )
        self.min_length = configs.get("min_length") or SECRET_DEFAULT_MIN_LENGTH
        self.max_length = min(
            configs.get("max_length") or SECRET_DEFAULT_MAX_LENGTH,
            SECRET_MAX_LENGTH_CAP,
        )
        self.multiline = bool(configs.get("multiline"))

    @property
    def customer_sets(self):
        return self.source == SECRET_SOURCE_CUSTOMER

    def clean(self, value):
        """Check a secret value without ever echoing it; returns it unchanged."""
        if not isinstance(value, str):
            raise serializers.ValidationError(_("Enter the secret as text."))
        if not self.min_length <= len(value) <= self.max_length:
            raise serializers.ValidationError(
                _("Enter %(min)s to %(max)s characters.")
                % {"min": self.min_length, "max": self.max_length}
            )
        if self.multiline:
            if _MULTILINE_CONTROL_CHARACTERS.search(value):
                raise serializers.ValidationError(
                    _("The secret may not hold control characters.")
                )
        elif _CONTROL_CHARACTERS.search(value):
            raise serializers.ValidationError(
                _("The secret must be a single line, without control characters.")
            )
        register_secret_value(value)
        return value


def validate_secret_policy(attrs):
    """Check a secret policy as an offering saves it."""
    errors = {}
    low = attrs.get("min_length", SECRET_DEFAULT_MIN_LENGTH)
    high = attrs.get("max_length", SECRET_DEFAULT_MAX_LENGTH)
    if high > SECRET_MAX_LENGTH_CAP:
        errors["max_length"] = _("Enter at most %s.") % SECRET_MAX_LENGTH_CAP
    elif low > high:
        errors["max_length"] = _(
            "The longest length must not be lower than the shortest."
        )
    if errors:
        raise serializers.ValidationError(errors)
    return attrs


class SecretPolicySerializer(serializers.Serializer):
    """The rules of a secret option (its default_configs)."""

    secret_source = serializers.ChoiceField(
        choices=SECRET_SOURCES,
        required=False,
        help_text=_(
            "Who sets the secret: the customer (when ordering, or later) or the "
            "provider and its site agent. customer when omitted."
        ),
    )
    customer_can_reveal = serializers.BooleanField(
        required=False,
        help_text=_(
            "Whether users holding RESOURCE.REVEAL_API_KEY on the resource's "
            "project or organization may read the secret back. When omitted: "
            "no for a customer-set secret, yes for a provider-set one."
        ),
    )
    min_length = serializers.IntegerField(
        min_value=1, required=False, help_text=_("1 when omitted.")
    )
    max_length = serializers.IntegerField(
        min_value=1,
        required=False,
        help_text=_("4096 when omitted; at most 65536."),
    )
    multiline = serializers.BooleanField(
        required=False,
        help_text=_(
            "Whether the secret may span lines, such as a PEM key or a "
            "kubeconfig. Off when omitted."
        ),
    )

    def validate(self, attrs):
        return validate_secret_policy(attrs)


class SecretField(serializers.Field):
    """A secret the customer enters, checked against the option's policy.

    Never stored with the other values: validate_options drops it, and the
    caller stores it encrypted (see get_submitted_secrets). An empty value
    sets nothing. A provider-sourced secret refuses any value from here.
    """

    def __init__(self, *args, **kwargs):
        self.policy = SecretPolicy(kwargs.pop("default_configs", None))
        self.trust_provider_end = kwargs.pop("trust_provider_end", False)
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        if data == "":
            if self.required:
                raise serializers.ValidationError(_("Enter a value."))
            return data
        if self.trust_provider_end:
            raise serializers.ValidationError(
                _("Set this secret with the set_option_secret action.")
            )
        if not self.policy.customer_sets:
            raise serializers.ValidationError(
                _("The provider sets this secret; leave it out.")
            )
        return self.policy.clean(data)

    def to_representation(self, value):
        # A stored secret is never shown; only whether it is set.
        return None


def is_ipsec_option(option):
    return (
        isinstance(option, dict) and option.get("type") == IPSEC_VPN_CONFIG_FIELD_TYPE
    )


def is_secret_option(option):
    return isinstance(option, dict) and option.get("type") == SECRET_FIELD_TYPE


def get_submitted_secrets(options, attributes, hidden=None):
    """The values sent for secret options, by option name.

    Call it once validate_options has accepted the same values: it does not
    check them again. Hidden options and empty values give none.
    """
    if not isinstance(attributes, dict) or not options:
        return {}
    if hidden is None:
        hidden = get_hidden_options(options, attributes)
    return {
        name: attributes[name]
        for name, option in options.items()
        if name not in hidden
        and is_secret_option(option)
        and isinstance(attributes.get(name), str)
        and attributes[name] != ""
    }


def strip_secret_options(values, names):
    """``values`` without the values of the secret options ``names``.

    A secret is never stored with the other values, even under an option
    nothing validated.
    """
    if not isinstance(values, dict) or not names or not set(names) & set(values):
        return values
    return {key: value for key, value in values.items() if key not in names}


def check_network_overlaps(fields, validated, stored):
    """Refuse remote subnets overlapping the cidr options their option names.

    ``fields`` are the option fields by name, ``validated`` the submitted
    values cleaned, and ``stored`` the values already stored for the order or
    resource, which stand in for options not submitted. A pair is checked only
    when one side of it was submitted, so a change elsewhere is not blocked.
    """
    stored = stored if isinstance(stored, dict) else {}
    for name, field in fields.items():
        if not isinstance(field, IpsecVpnConfigField):
            continue
        vpn = validated[name] if name in validated else stored.get(name)
        if not isinstance(vpn, dict):
            continue
        remote = _policy_networks(vpn.get("remote_subnets"))
        for other in field.policy.no_overlap_with:
            if name not in validated and other not in validated:
                continue
            networks = cidr_value_networks(
                validated[other] if other in validated else stored.get(other)
            )
            for subnet in remote:
                for network in networks:
                    if not _overlaps(subnet, network):
                        continue
                    message = _("%(subnet)s overlaps %(network)s of %(option)s.") % {
                        "subnet": subnet,
                        "network": network,
                        "option": other,
                    }
                    if name in validated:
                        raise serializers.ValidationError(
                            {name: {"remote_subnets": [message]}}
                        )
                    raise serializers.ValidationError({other: [message]})


FIELD_CLASSES = {
    "integer": serializers.IntegerField,
    "date": serializers.DateField,
    "time": serializers.TimeField,
    "money": serializers.IntegerField,
    "boolean": serializers.BooleanField,
    "select_string": serializers.ChoiceField,
    "select_string_multi": serializers.MultipleChoiceField,
    "select_openstack_tenant": serializers.CharField,
    "select_multiple_openstack_tenants": StringListSerializer,
    "select_openstack_instance": serializers.CharField,
    "select_multiple_openstack_instances": StringListSerializer,
    "select_multiple_emails": EmailListSerializer,
    "conditional_cascade": ConditionalCascadeField,
    "component_multiplier": serializers.IntegerField,
    "component_formula": serializers.IntegerField,
    "storage_folder_manager": StorageFolderManagerField,
    K8S_CLUSTER_CONFIG_FIELD_TYPE: K8sConfigField,
    K8S_EGRESS_RULES_FIELD_TYPE: K8sEgressRulesField,
    CIDR_FIELD_TYPE: CidrField,
    IPSEC_VPN_CONFIG_FIELD_TYPE: IpsecVpnConfigField,
    SECRET_FIELD_TYPE: SecretField,
}


# Fields whose cleaned value is stored instead of the submitted one.
CLEANED_FIELD_CLASSES = (
    K8sConfigField,
    K8sEgressRulesField,
    CidrField,
    IpsecVpnConfigField,
)
# Fields that take the option's policy from its default_configs.
POLICY_FIELD_CLASSES = (*CLEANED_FIELD_CLASSES, SecretField)

# Option types the customer never fills in; any value sent for one is dropped.
NON_INPUT_FIELD_TYPES = frozenset({"component_sum"})


# Option types whose value can decide whether another option is shown.
VISIBLE_IF_FIELD_TYPES = ("boolean", "select_string", "select_string_multi")

# Option types whose value can be checked against a provider-defined pattern.
PATTERN_FIELD_TYPES = ("string", "text")
MAX_PATTERN_LENGTH = 500
# Patterns come from providers and run on every order, so a pathological one
# (catastrophic backtracking) must give up instead of hanging a worker.
PATTERN_MATCH_TIMEOUT = 0.1


def compile_option_pattern(pattern):
    """Compile a provider-defined option pattern.

    ASCII mode gives \\w, \\d, \\s and \\b the meaning they have in JavaScript,
    so the check Homeport repeats in the order form accepts the same values.
    Raises regex.error for an invalid pattern.
    """
    return regex.compile(pattern, flags=regex.ASCII)


def option_pattern_matches(pattern, value):
    """Whether the whole value matches the pattern.

    Raises regex.error for an invalid pattern and TimeoutError when matching
    takes longer than PATTERN_MATCH_TIMEOUT seconds.
    """
    compiled = compile_option_pattern(pattern)
    return compiled.fullmatch(value, timeout=PATTERN_MATCH_TIMEOUT) is not None


class OptionPatternValidator:
    def __init__(self, name, pattern, message=None):
        self.name = name
        self.pattern = pattern
        self.message = message or _("Value must match the pattern %(pattern)s.") % {
            "pattern": pattern
        }

    def __call__(self, value):
        try:
            matches = option_pattern_matches(self.pattern, value)
        except regex.error:
            # Patterns are compiled when the offering is saved, so this is
            # only reachable through data written around the serializer.
            raise serializers.ValidationError(
                _(
                    "The validation pattern of this option is invalid. "
                    "Please contact the service provider."
                )
            )
        except TimeoutError:
            # The customer cannot fix this, so make it visible to operators.
            logger.warning(
                "Pattern of offering option %s timed out after %ss on a "
                "%s-character value: %r",
                self.name,
                PATTERN_MATCH_TIMEOUT,
                len(value),
                self.pattern,
            )
            raise serializers.ValidationError(
                _(
                    "Value could not be checked against the pattern in time. "
                    "Please contact the service provider."
                )
            )
        if not matches:
            raise serializers.ValidationError(self.message)


# Option types whose value can be rewritten before it is stored.
TRANSFORM_FIELD_TYPES = PATTERN_FIELD_TYPES
TRANSFORM_CASES = ("lower", "upper")
MAX_TRANSFORM_AFFIX_LENGTH = 100


def strip_option_affixes(transform, value):
    """The part of a value the user typed, without the transform's prefix and suffix.

    A stored value comes back already transformed when a pending order or a
    resource's options are edited, so the prefix and suffix are removed if the
    value carries them. That keeps transform_option_value from adding them twice.
    With a case set, the user may type the prefix in either case ("ACME-logs"),
    so the affixes are matched ignoring case.
    """
    prefix = transform.get("prefix") or ""
    suffix = transform.get("suffix") or ""

    def fold(text):
        return text.lower() if transform.get("case") else text

    if prefix and fold(value[: len(prefix)]) == fold(prefix):
        value = value[len(prefix) :]
    if (
        suffix
        and len(value) >= len(suffix)
        and fold(value[-len(suffix) :]) == fold(suffix)
    ):
        value = value[: -len(suffix)]
    return value


def transform_option_value(transform, value):
    """The value to store for a submitted one, by the option's transform.

    Applying it to its own result gives the same value. The case applies only
    to the part the user typed: the prefix and suffix are kept as written.
    """
    value = strip_option_affixes(transform, value)
    case = transform.get("case")
    if case == "lower":
        value = value.lower()
    elif case == "upper":
        value = value.upper()
    return "{}{}{}".format(
        transform.get("prefix") or "", value, transform.get("suffix") or ""
    )


def get_option_transforms(options, values, hidden=()):
    """The transforms that apply to the submitted values, by option name.

    Only string and text options with a transform and a typed value qualify.
    """
    if not isinstance(values, dict):
        return {}
    return {
        name: option["transform"]
        for name, option in options.items()
        if name not in hidden
        and option.get("type") in TRANSFORM_FIELD_TYPES
        and option.get("transform")
        and isinstance(values.get(name), str)
        and values[name] != ""
    }


def transform_option_values(options, values, hidden=()):
    """The values with each option's transform applied, without validating them.

    For values that were stored before they were transformed: as the transform
    gives the same result twice, already transformed values are kept.
    """
    transforms = get_option_transforms(options, values, hidden)
    if not transforms:
        return values
    return {
        **values,
        **{
            name: transform_option_value(transform, values[name])
            for name, transform in transforms.items()
        },
    }


def _option_value_matches(option, value, values):
    """Whether a submitted option value satisfies a visible_if rule."""
    field_type = option.get("type")
    if field_type == "boolean":
        # An unticked checkbox is not submitted at all, so a missing value
        # means false; otherwise a "show when unchecked" rule never matches.
        if value is None:
            value = False
        try:
            value = serializers.BooleanField().to_internal_value(value)
        except serializers.ValidationError:
            return False
        return any(isinstance(item, bool) and item is value for item in values)
    if value is None:
        return False
    if field_type == "select_string":
        return isinstance(value, str) and value in values
    if field_type == "select_string_multi":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list | tuple | set):
            return False
        return any(isinstance(item, str) and item in values for item in value)
    return False


def get_hidden_options(options, values):
    """Names of the options hidden by their visible_if rules.

    An option with a visible_if rule is shown only when the option it refers
    to is itself shown and its value matches one of the rule's values. A hidden
    option counts as having no value, so hiding cascades down a chain. A rule
    that refers to an unknown option, or that is part of a cycle, hides the
    option; both are rejected when the offering is saved.

    Rules look only at the submitted values: a parent's `default` does not
    count, and the frontend does not apply option defaults either.
    """
    if not isinstance(values, dict):
        values = {}
    visibility = {}

    def as_dict(option):
        return option if isinstance(option, dict) else {}

    def is_visible(name, path):
        if name in visibility:
            return visibility[name]
        rule = as_dict(options[name]).get("visible_if")
        if not rule:
            result = True
        else:
            parent = rule.get("field")
            if parent not in options or parent in path:
                result = False
            else:
                result = is_visible(parent, path | {name}) and _option_value_matches(
                    as_dict(options[parent]),
                    values.get(parent),
                    rule.get("values") or [],
                )
        visibility[name] = result
        return result

    return {name for name in options if not is_visible(name, frozenset({name}))}


def validate_options(
    options,
    attributes,
    optional=False,
    hidden=None,
    stored=None,
    trust_provider_end=False,
):
    """Validate submitted option values and return the values to store.

    Hidden options are dropped, and the values of options with a `transform`
    are rewritten (see transform_option_value). Their pattern is checked
    against the part the user typed, before the transform.

    `hidden` is the set of hidden option names. When omitted it is worked out
    from `attributes`; callers that validate a partial update pass the set
    computed from the merged values instead.

    `stored` holds the values already stored for the order or resource being
    changed: an ipsec_vpn_config option keeps its provider end from them, and
    no_overlap_with checks against them the options not submitted. With
    `trust_provider_end`, the provider's own update, a submitted provider end
    replaces the stored one.
    """
    if hidden is None:
        hidden = get_hidden_options(options, attributes)
    transforms = get_option_transforms(options, attributes, hidden)
    data = attributes
    if transforms:
        data = {
            **attributes,
            **{
                name: strip_option_affixes(transform, attributes[name])
                for name, transform in transforms.items()
            },
        }
        # Only the prefix or suffix leaves nothing typed; say so rather than
        # report the field as blank, as the user did type something.
        affix_only = {
            name: _("Enter a value besides the prefix and suffix.")
            for name in transforms
            if data[name] == ""
        }
        if affix_only:
            raise serializers.ValidationError(affix_only)
    fields = {}

    for name, option in options.items():
        if name in hidden:
            continue
        params = {}
        field_type = option.get("type", "")
        if field_type in NON_INPUT_FIELD_TYPES:
            continue
        field_class = FIELD_CLASSES.get(field_type, serializers.CharField)

        default_value = option.get("default")
        if default_value:
            params["default"] = default_value
        else:
            params["required"] = option.get("required", False)
        if optional:
            params["required"] = False

        if field_class == serializers.IntegerField:
            if "min" in option:
                params["min_value"] = option.get("min")

            if "max" in option:
                params["max_value"] = option.get("max")

        if "choices" in option:
            # Only add choices parameter to field types that support it
            if field_class in (
                serializers.ChoiceField,
                serializers.MultipleChoiceField,
            ):
                params["choices"] = option["choices"]

        if field_type in PATTERN_FIELD_TYPES and option.get("pattern"):
            params["validators"] = [
                OptionPatternValidator(
                    name, option["pattern"], option.get("pattern_error")
                )
            ]
            # The submitted value is stored as is, so check it untrimmed.
            params["trim_whitespace"] = False

        if issubclass(field_class, POLICY_FIELD_CLASSES):
            params["default_configs"] = option.get("default_configs")
        if field_class is IpsecVpnConfigField:
            params["stored_value"] = (stored or {}).get(name)
            params["trust_provider_end"] = trust_provider_end
        if field_class is SecretField:
            params["trust_provider_end"] = trust_provider_end
            # Never a default value; and a secret the provider sets is not
            # the customer's to give, required or not.
            params.pop("default", None)
            params["required"] = (
                bool(option.get("required"))
                and not optional
                and SecretPolicy(option.get("default_configs")).customer_sets
            )

        fields[name] = field_class(**params)

    validators = []
    for name, option in options.items():
        if name in hidden:
            continue
        if "validators" in option:
            for validator in option["validators"]:
                if validator.get("target_field") in hidden:
                    continue
                validator["source_field"] = name
                validators.append(validator)

    if validators:

        def validate(self, attrs):
            for validator in validators:
                source = attrs.get(validator["source_field"])
                target = attrs.get(validator["target_field"])
                if source is None or target is None:
                    continue

                error_msg = None
                if validator["type"] == "gt" and source <= target:
                    error_msg = f"{validator['source_field']} must be greater than {validator['target_field']}"
                elif validator["type"] == "gte" and source < target:
                    error_msg = f"{validator['source_field']} must be greater than or equal to {validator['target_field']}"
                elif validator["type"] == "lt" and source >= target:
                    error_msg = f"{validator['source_field']} must be less than {validator['target_field']}"
                elif validator["type"] == "lte" and source > target:
                    error_msg = f"{validator['source_field']} must be less than or equal to {validator['target_field']}"

                if error_msg:
                    raise serializers.ValidationError(
                        {validator["source_field"]: error_msg}
                    )
            return attrs

        fields["validate"] = validate

    serializer_class = type("AttributesSerializer", (serializers.Serializer,), fields)
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    check_network_overlaps(serializer.fields, serializer.validated_data, stored)

    values = strip_hidden_options(
        options, strip_non_input_options(options, attributes), hidden
    )
    # Values whose field cleans them are stored cleaned, so that consumers
    # such as a provider's automation get the checked shape.
    for name, field in serializer.fields.items():
        if isinstance(field, CLEANED_FIELD_CLASSES) and name in values:
            values[name] = serializer.validated_data.get(name, values[name])
    # A secret is never stored with the values; the caller takes it from the
    # submitted ones (get_submitted_secrets) and stores it encrypted.
    values = strip_secret_options(
        values, [name for name, option in options.items() if is_secret_option(option)]
    )
    return transform_option_values(options, values, hidden)


def strip_non_input_options(options, values):
    """Drop values sent for options the customer never fills in."""
    if not isinstance(values, dict):
        return values
    return {
        key: value
        for key, value in values.items()
        if (options.get(key) or {}).get("type") not in NON_INPUT_FIELD_TYPES
    }


def strip_hidden_options(options, values, hidden=None):
    """Drop the values of hidden options from a dict of option values."""
    if not isinstance(values, dict):
        return values
    if hidden is None:
        hidden = get_hidden_options(options, values)
    if not hidden:
        return values
    return {key: value for key, value in values.items() if key not in hidden}
