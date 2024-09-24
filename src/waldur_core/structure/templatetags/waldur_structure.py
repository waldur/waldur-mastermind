from collections import OrderedDict

from django import template

from waldur_core.core.utils import get_fake_context

register = template.Library()


def get_fields(serializer_class):
    fields = OrderedDict()
    extra_fields = OrderedDict()

    serializer = serializer_class(context=get_fake_context())
    for name, field in serializer.get_fields().items():
        data = {
            "label": field.label,
            "help_text": field.help_text,
            "required": field.required,
        }
        if field.source:
            extra_fields[name] = data
        else:
            fields[name] = data

    return (fields, extra_fields)
