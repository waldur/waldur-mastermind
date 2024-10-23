import logging
import re

from croniter import croniter
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat import backends as hazmat_backends
from cryptography.hazmat.primitives import serialization as hazmat_serialization
from django import template
from django.core.exceptions import ValidationError
from django.core.validators import BaseValidator, URLValidator
from django.utils import timezone
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _
from iptools.ipv4 import validate_cidr as is_valid_ipv4_cidr
from iptools.ipv6 import validate_cidr as is_valid_ipv6_cidr

from waldur_core.core import exceptions

logger = logging.getLogger(__name__)


PHONE_REGEX = re.compile(r"^\+?[\d \-\(\)]+$")


def validate_phone_number(value):
    if not PHONE_REGEX.search(value):
        raise ValidationError("Invalid phone number format.")


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

    message = _(
        "Ensure schedule period is greater than or equal to %(limit_value)s hour(s)."
    )
    code = "min_cron_value"

    def compare(self, cleaned, limit_value):
        validate_cron_schedule(cleaned)

        now = timezone.now()
        schedule = croniter(cleaned, now)
        closest_schedule = schedule.get_next(timezone.datetime)
        next_schedule = schedule.get_next(timezone.datetime)
        schedule_interval_in_hours = (
            next_schedule - closest_schedule
        ).total_seconds() / 3600
        return schedule_interval_in_hours < limit_value


def validate_name(value):
    if len(value.strip()) == 0:
        raise ValidationError(
            _("Ensure that name has at least one non-whitespace character.")
        )


class StateValidator:
    def __init__(self, *valid_states):
        self.valid_states = valid_states

    def __call__(self, resource):
        if resource.state not in self.valid_states:
            states_names = dict(resource.States.CHOICES)
            valid_states_names = [
                str(states_names[state]) for state in self.valid_states
            ]
            raise exceptions.IncorrectStateException(
                _("Valid states for operation: %s.") % ", ".join(valid_states_names)
            )


class RuntimeStateValidator(StateValidator):
    def __call__(self, resource):
        if resource.runtime_state not in self.valid_states:
            raise exceptions.IncorrectStateException(
                _("Valid runtime states for operation: %s.")
                % ", ".join(self.valid_states)
            )


class BackendURLValidator(URLValidator):
    schemes = ["ldap", "ldaps", "http", "https", "ssh", "rdp"]


def is_valid_ipv46_cidr(value):
    return is_valid_ipv6_cidr(value) or is_valid_ipv4_cidr(value)


def validate_cidr_list(value):
    if not value.strip():
        return
    invalid_items = []
    for item in value.split(","):
        item = item.strip()
        if not is_valid_ipv46_cidr(item):
            invalid_items.append(item)
    if invalid_items:
        raise ValidationError(
            message=_("The following items are invalid: %s"),
            code="invalid",
            params=", ".join(invalid_items),
        )


@deconstructible
class BlacklistValidator:
    message = _("This value is blacklisted.")
    code = "blacklist"
    blacklist = ()

    def __init__(self, blacklist=None, message=None, code=None):
        if blacklist is not None:
            self.blacklist = blacklist
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code

    def __call__(self, value):
        if value in self.blacklist:
            raise ValidationError(self.message, code=self.code)


def validate_template_syntax(value):
    try:
        template.Template(value)
    except template.exceptions.TemplateSyntaxError as e:
        raise ValidationError(e)


def validate_ssh_public_key(ssh_key):
    if isinstance(ssh_key, str):
        ssh_key = ssh_key.encode("utf-8")

    try:
        hazmat_serialization.load_ssh_public_key(
            ssh_key, hazmat_backends.default_backend()
        )
    except (ValueError, UnsupportedAlgorithm) as e:
        logger.debug("Invalid SSH public key %s. Error: %s", ssh_key, e)
        raise ValidationError(_("Invalid SSH public key."))


def validate_x509_certificate(data):
    if isinstance(data, str):
        data = data.encode("utf-8")

    try:
        x509.load_pem_x509_certificate(data)
    except ValueError:
        raise ValidationError(_("Invalid X509 certificate."))
