from rest_framework import serializers


def validate_options(options, attributes):
    fields = {}

    for name, option in options.items():
        params = {}
        field_type = option.get('type', '')
        field_class = serializers.CharField

        if field_type == 'integer':
            field_class = serializers.IntegerField

        elif field_type == 'money':
            field_class = serializers.IntegerField

        elif field_type == 'boolean':
            field_class = serializers.BooleanField

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
            field_class = serializers.ChoiceField
            params['choices'] = option.get('choices')

        fields[name] = field_class(**params)

    serializer_class = type(b'AttributesSerializer', (serializers.Serializer,), fields)
    serializer = serializer_class(data=attributes)
    serializer.is_valid(raise_exception=True)
