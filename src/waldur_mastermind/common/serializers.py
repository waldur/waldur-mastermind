from django.core.validators import MinValueValidator
from rest_framework import serializers


class StringListSerializer(serializers.ListField):
    child = serializers.CharField()


class EmailListSerializer(serializers.ListField):
    child = serializers.EmailField()


class ConditionalCascadeField(serializers.DictField):
    """Field for conditional cascade selections that stores step-value mappings"""


class SingleDatacenterK8sConfigField(serializers.DictField):
    """Field for single-datacenter Kubernetes cluster configuration"""


class MultiDatacenterK8sConfigField(serializers.DictField):
    """Field for multi-datacenter Kubernetes cluster configuration"""


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
    "storage_folder_manager": StorageFolderManagerField,
    "single_datacenter_k8s_config": SingleDatacenterK8sConfigField,
    "multi_datacenter_k8s_config": MultiDatacenterK8sConfigField,
}


# Option types whose value can decide whether another option is shown.
VISIBLE_IF_FIELD_TYPES = ("boolean", "select_string", "select_string_multi")


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
    """Validate submitted option values and return them without hidden options.

    `hidden` is the set of hidden option names. When omitted it is worked out
    from `attributes`; callers that validate a partial update pass the set
    computed from the merged values instead.
    """
    if hidden is None:
        hidden = get_hidden_options(options, attributes)
    fields = {}

    for name, option in options.items():
        if name in hidden:
            continue
        params = {}
        field_type = option.get("type", "")
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
    serializer = serializer_class(data=attributes)
    serializer.is_valid(raise_exception=True)

    return strip_hidden_options(options, attributes, hidden)


def strip_hidden_options(options, values, hidden=None):
    """Drop the values of hidden options from a dict of option values."""
    if not isinstance(values, dict):
        return values
    if hidden is None:
        hidden = get_hidden_options(options, values)
    if not hidden:
        return values
    return {key: value for key, value in values.items() if key not in hidden}
