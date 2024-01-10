from collections import OrderedDict
from functools import lru_cache

from django import template

from waldur_core.core.utils import get_fake_context
from waldur_core.structure.registry import get_service_type
from waldur_core.structure.serializers import ServiceOptionsSerializer

register = template.Library()


@lru_cache(maxsize=1)
@register.inclusion_tag("structure/service_settings_description.html")
def service_settings_description():
    services = []
    for cls in ServiceOptionsSerializer.get_subclasses():
        name = get_service_type(cls)
        if not name:
            continue
        fields, extra_fields = get_fields(cls)
        services.append((name, {"fields": fields, "extra_fields": extra_fields}))
    return {"services": sorted(services)}


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
