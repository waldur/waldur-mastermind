from rest_framework import serializers


class StringListSerializer(serializers.ListField):
    child = serializers.CharField()


FIELD_CLASSES = {
    'integer': serializers.IntegerField,
    'date': serializers.DateField,
    'time': serializers.TimeField,
    'money': serializers.IntegerField,
    'boolean': serializers.BooleanField,
    'select_string': serializers.ChoiceField,
    'select_string_multi': serializers.MultipleChoiceField,
    'select_openstack_tenant': serializers.CharField,
    'select_multiple_openstack_tenants': StringListSerializer,
    'select_openstack_instance': serializers.CharField,
    'select_multiple_openstack_instances': StringListSerializer,
}


def validate_options(options, attributes):
    fields = {}

    for name, option in options.items():
        params = {}
        field_type = option.get('type', '')
        field_class = FIELD_CLASSES.get(field_type, serializers.CharField)

        default_value = option.get('default')
        if default_value:
            params['default'] = default_value
        else:
            params['required'] = option.get('required', False)

        if field_class == serializers.IntegerField:
            if 'min' in option:
                params['min_value'] = option.get('min')

            if 'max' in option:
                params['max_value'] = option.get('max')

        if 'choices' in option:
            params['choices'] = option['choices']

        fields[name] = field_class(**params)

    serializer_class = type('AttributesSerializer', (serializers.Serializer,), fields)
    serializer = serializer_class(data=attributes)
    serializer.is_valid(raise_exception=True)
