import ipaddress
import logging
import re

import regex
from django.core.validators import MinValueValidator
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

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


K8S_CONFIG_FIELD_TYPES = ("single_datacenter_k8s_config", "multi_datacenter_k8s_config")
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


def validate_egress_destination(destination_type, destination):
    """Raise a ValidationError unless ``destination`` is a valid value of its type."""
    if destination_type == K8S_EGRESS_DESTINATION_CIDR:
        try:
            if not _CIDR_SHAPE.match(destination):
                raise ValueError(destination)
            ipaddress.ip_network(destination, strict=False)
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
    when the option does not set it, the option type decides. Values that lack
    the topology or datacenters keys are left alone, so orders placed before
    these checks existed still validate.

    The egress_rules list is checked against the offering's egress policy
    (see K8sEgressRulesField).
    """

    default_topology = None

    def __init__(self, *args, **kwargs):
        self.default_configs = kwargs.pop("default_configs", None) or {}
        self.topology_mode = (
            self.default_configs.get("topology_mode") or self.default_topology
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


class SingleDatacenterK8sConfigField(K8sConfigField):
    """Field for single-datacenter Kubernetes cluster configuration"""

    default_topology = K8S_TOPOLOGY_ONE_DATACENTER


class MultiDatacenterK8sConfigField(K8sConfigField):
    """Field for multi-datacenter Kubernetes cluster configuration"""

    default_topology = K8S_TOPOLOGY_THREE_DATACENTERS


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
    "single_datacenter_k8s_config": SingleDatacenterK8sConfigField,
    "multi_datacenter_k8s_config": MultiDatacenterK8sConfigField,
    K8S_EGRESS_RULES_FIELD_TYPE: K8sEgressRulesField,
}


# Fields whose cleaned value is stored instead of the submitted one.
CLEANED_FIELD_CLASSES = (K8sConfigField, K8sEgressRulesField)

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


def validate_options(options, attributes, optional=False, hidden=None):
    """Validate submitted option values and return the values to store.

    Hidden options are dropped, and the values of options with a `transform`
    are rewritten (see transform_option_value). Their pattern is checked
    against the part the user typed, before the transform.

    `hidden` is the set of hidden option names. When omitted it is worked out
    from `attributes`; callers that validate a partial update pass the set
    computed from the merged values instead.
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

        if issubclass(field_class, K8sConfigField | K8sEgressRulesField):
            params["default_configs"] = option.get("default_configs")

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

    values = strip_hidden_options(
        options, strip_non_input_options(options, attributes), hidden
    )
    # Values whose field cleans them are stored cleaned, so that consumers
    # such as a provider's automation get the checked shape.
    for name, field in serializer.fields.items():
        if isinstance(field, CLEANED_FIELD_CLASSES) and name in values:
            values[name] = serializer.validated_data.get(name, values[name])
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
