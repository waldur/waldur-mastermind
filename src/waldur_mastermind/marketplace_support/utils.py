from rest_framework import exceptions as rf_exceptions
from django.utils.translation import ugettext_lazy as _


def validate_options(attr):
    options = attr.get('options')
    if not options:
        raise rf_exceptions.ValidationError({
            'options': _('For selected type this field is required.')
        })

    if not isinstance(options, dict):
        raise rf_exceptions.ValidationError({
            'options': _('Dictionary is expected.')
        })
