from rest_framework import serializers


def validate_options(options, attributes):
    fields = {}

    for name, option in options.items():
        field_type = option.get('type', '')

        if field_type == 'string':
            field = serializers.CharField()

        elif field_type == 'integer':
            field = serializers.IntegerField()

        elif field_type == 'money':
            field = serializers.IntegerField()

        elif field_type == 'boolean':
            field = serializers.BooleanField()

        else:
            field = serializers.CharField()

        default_value = option.get('default')
        if default_value:
            field.default = default_value

        if 'min' in option:
            field.min_value = option.get('min')

        if 'max' in option:
            field.max_value = option.get('max')

        if 'choices' in option:
            fields.choices = option.get('choices')

        field.required = option.get('required', False)
        field.label = option.get('label')
        field.help_text = option.get('help_text')

        fields[name] = field

    serializer_class = type(b'AttributesSerializer', (serializers.Serializer,), fields)
    serializer = serializer_class(data=attributes)
    serializer.is_valid(raise_exception=True)
