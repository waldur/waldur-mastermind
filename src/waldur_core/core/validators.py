from __future__ import unicode_literals

import os

from croniter import croniter
from django.core.exceptions import ValidationError
from django.core.validators import BaseValidator, URLValidator
from django.utils import timezone
from django.utils.deconstruct import deconstructible
from django.utils.translation import ugettext_lazy as _
from iptools.ipv4 import validate_cidr as is_valid_ipv4_cidr
from iptools.ipv6 import validate_cidr as is_valid_ipv6_cidr

from waldur_core.core import exceptions
from waldur_core.core import magic


def validate_cron_schedule(value):
    try:
        base_time = timezone.now()
        croniter(value, base_time)
    except (KeyError, ValueError) as e:
        raise ValidationError(str(e))


@deconstructible
class MinCronValueValidator(BaseValidator):
    """
    Validate that the period of cron schedule is greater than or equal to provided limit_value in hours,
    otherwise raise ValidationError.
    """
    message = _('Ensure schedule period is greater than or equal to %(limit_value)s hour(s).')
    code = 'min_cron_value'

    def compare(self, cleaned, limit_value):
        validate_cron_schedule(cleaned)

        now = timezone.now()
        schedule = croniter(cleaned, now)
        closest_schedule = schedule.get_next(timezone.datetime)
        next_schedule = schedule.get_next(timezone.datetime)
        schedule_interval_in_hours = (next_schedule - closest_schedule).total_seconds() / 3600
        return schedule_interval_in_hours < limit_value


def validate_name(value):
    if len(value.strip()) == 0:
        raise ValidationError(_('Ensure that name has at least one non-whitespace character.'))


class StateValidator(object):

    def __init__(self, *valid_states):
        self.valid_states = valid_states

    def __call__(self, resource):
        if resource.state not in self.valid_states:
            states_names = dict(resource.States.CHOICES)
            valid_states_names = [str(states_names[state]) for state in self.valid_states]
            raise exceptions.IncorrectStateException(_('Valid states for operation: %s.') % ', '.join(valid_states_names))


class RuntimeStateValidator(StateValidator):

    def __call__(self, resource):
        if resource.runtime_state not in self.valid_states:
            raise exceptions.IncorrectStateException(_('Valid runtime states for operation: %s.') % ', '.join(self.valid_states))


class BackendURLValidator(URLValidator):
    schemes = ['ldap', 'ldaps', 'http', 'https']


def is_valid_ipv46_cidr(value):
    return is_valid_ipv6_cidr(value) or is_valid_ipv4_cidr(value)


def validate_cidr_list(value):
    if not value.strip():
        return
    invalid_items = []
    for item in value.split(','):
        item = item.strip()
        if not is_valid_ipv46_cidr(item):
            invalid_items.append(item)
    if invalid_items:
        raise ValidationError(
            message=_('The following items are invalid: %s'),
            code='invalid',
            params=', '.join(invalid_items),
        )


# max bytes to read for file type detection
READ_SIZE = 5 * (1024 * 1024)   # 5MB


# Based on https://github.com/mckinseyacademy/django-upload-validator/blob/master/upload_validator/__init__.py
@deconstructible
class FileTypeValidator(object):
    """
    File type validator for validating mimetypes and extensions
    Args:
        allowed_types (list): list of acceptable mimetypes e.g; ['image/jpeg', 'application/pdf']
                    see https://www.iana.org/assignments/media-types/media-types.xhtml
        allowed_extensions (list, optional): list of allowed file extensions e.g; ['.jpeg', '.pdf', '.docx']
    """
    type_message = _(
        "File type '%(detected_type)s' is not allowed. "
        "Allowed types are: '%(allowed_types)s'."
    )

    extension_message = _(
        "File extension '%(extension)s' is not allowed. "
        "Allowed extensions are: '%(allowed_extensions)s'."
    )

    def __init__(self, allowed_types, allowed_extensions=()):
        self.allowed_mimes = allowed_types
        self.allowed_exts = allowed_extensions

    def __call__(self, fileobj):
        detected_type = magic.from_buffer(fileobj.read(READ_SIZE), mime=True)
        root, extension = os.path.splitext(fileobj.name.lower())

        # seek back to start so a valid file could be read
        # later without resetting the position
        fileobj.seek(0)

        # some versions of libmagic do not report proper mimes for Office subtypes
        # use detection details to transform it to proper mime
        if detected_type in ('application/octet-stream', 'application/vnd.ms-office'):
            detected_type = self.check_word_or_excel(fileobj, detected_type, extension)

        if detected_type not in self.allowed_mimes:
            # use more readable file type names for feedback message
            allowed_types = map(lambda mime_type: mime_type.split('/')[1], self.allowed_mimes)

            raise ValidationError(
                message=self.type_message,
                params={
                    'detected_type': detected_type,
                    'allowed_types': ', '.join(allowed_types)
                },
                code='invalid_type'
            )

        if self.allowed_exts and (extension not in self.allowed_exts):
            raise ValidationError(
                message=self.extension_message,
                params={
                    'extension': extension,
                    'allowed_extensions': ', '.join(self.allowed_exts)
                },
                code='invalid_extension'
            )

    def check_word_or_excel(self, fileobj, detected_type, extension):
        """
        Returns proper mimetype in case of word or excel files
        """
        word_strings = ['Microsoft Word', 'Microsoft Office Word', 'Microsoft Macintosh Word']
        excel_strings = ['Microsoft Excel', 'Microsoft Office Excel', 'Microsoft Macintosh Excel']
        office_strings = ['Microsoft OOXML']

        file_type_details = magic.from_buffer(fileobj.read(READ_SIZE))

        fileobj.seek(0)

        if any(string in file_type_details for string in word_strings):
            detected_type = 'application/msword'
        elif any(string in file_type_details for string in excel_strings):
            detected_type = 'application/vnd.ms-excel'
        elif any(string in file_type_details for string in office_strings) or \
                (detected_type == 'application/vnd.ms-office'):
            if extension in ('.doc', '.docx'):
                detected_type = 'application/msword'
            if extension in ('.xls', '.xlsx'):
                detected_type = 'application/vnd.ms-excel'

        return detected_type


ImageValidator = FileTypeValidator(
    allowed_types=[
        'image/png',
        'image/jpeg',
        'image/svg',
        'image/svg+xml',
    ]
)
