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


def validate_options(options, attributes, optional=False):
    fields = {}

    for name, option in options.items():
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
        if "validators" in option:
            for validator in option["validators"]:
                validator["source_field"] = name
                validators.append(validator)

    if validators:

        def validate(self, attrs):
            for validator in validators:
                if validator["type"] == "gt":
                    source = attrs.get(validator["source_field"])
                    target = attrs.get(validator["target_field"])
                    if source is not None and target is not None and source <= target:
                        raise serializers.ValidationError(
                            {
                                validator[
                                    "source_field"
                                ]: f"{validator['source_field']} must be greater than {validator['target_field']}"
                            }
                        )
            return attrs

        fields["validate"] = validate

    serializer_class = type("AttributesSerializer", (serializers.Serializer,), fields)
    serializer = serializer_class(data=attributes)
    serializer.is_valid(raise_exception=True)
