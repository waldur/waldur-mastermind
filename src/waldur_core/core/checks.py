from django.conf import settings
from django.core.checks import Error, register
from pydantic import ValidationError

from waldur_core.core.metadata import WaldurConfiguration


@register()
def settings_are_valid(app_configs, **kwargs):
    errors = []
    try:
        WaldurConfiguration(**settings._wrapped.__dict__)
    except ValidationError as e:
        errors.append(Error('Settings are invalid', hint=e.json(), id='waldur.E001',))
    return errors
