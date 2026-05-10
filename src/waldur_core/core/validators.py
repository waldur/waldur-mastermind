import ipaddress
import logging
import re

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat import backends as hazmat_backends
from cryptography.hazmat.primitives import serialization as hazmat_serialization
from django import template
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

from waldur_core.core import exceptions
from waldur_core.core.enums import GENDER_CHOICES, CoreStates

logger = logging.getLogger(__name__)


PHONE_REGEX = re.compile(r"^\+?[\d \-\(\)]+$")


def validate_phone_number(value):
    if not PHONE_REGEX.search(value):
        raise ValidationError("Invalid phone number format.")


def validate_name(value):
    if len(value.strip()) == 0:
        raise ValidationError(
            _("Ensure that name has at least one non-whitespace character.")
        )


class StateValidator:
    # Use state_enum to validate states of a model that has custom state field (e.g. RobotAccounts use RobotAccountStates)
    def __init__(self, *valid_states, state_enum=None):
        self.state_enum = state_enum
        self.valid_states = valid_states

    def __call__(self, resource):
        if resource.state not in self.valid_states:
            if self.state_enum:
                states_names = dict(self.state_enum.CHOICES)
            elif hasattr(resource, "States"):
                states_names = dict(resource.States.CHOICES)
            else:
                states_names = dict(CoreStates.choices)
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


def is_valid_ipv4_cidr(value: str) -> bool:
    # Mirrors iptools.ipv4.validate_cidr: bare addresses without /prefix are rejected.
    if not isinstance(value, str) or "/" not in value:
        return False
    try:
        ipaddress.IPv4Network(value, strict=False)
    except ValueError:
        return False
    return True


def is_valid_ipv6_cidr(value: str) -> bool:
    if not isinstance(value, str) or "/" not in value:
        return False
    try:
        ipaddress.IPv6Network(value, strict=False)
    except ValueError:
        return False
    return True


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


# ISO 3166-1 alpha-2 country codes (common subset, can be extended)
# This is a subset - full validation can be done with pycountry if needed
ISO_3166_1_ALPHA_2_CODES = {
    "AD",
    "AE",
    "AF",
    "AG",
    "AI",
    "AL",
    "AM",
    "AO",
    "AQ",
    "AR",
    "AS",
    "AT",
    "AU",
    "AW",
    "AX",
    "AZ",
    "BA",
    "BB",
    "BD",
    "BE",
    "BF",
    "BG",
    "BH",
    "BI",
    "BJ",
    "BL",
    "BM",
    "BN",
    "BO",
    "BQ",
    "BR",
    "BS",
    "BT",
    "BV",
    "BW",
    "BY",
    "BZ",
    "CA",
    "CC",
    "CD",
    "CF",
    "CG",
    "CH",
    "CI",
    "CK",
    "CL",
    "CM",
    "CN",
    "CO",
    "CR",
    "CU",
    "CV",
    "CW",
    "CX",
    "CY",
    "CZ",
    "DE",
    "DJ",
    "DK",
    "DM",
    "DO",
    "DZ",
    "EC",
    "EE",
    "EG",
    "EH",
    "ER",
    "ES",
    "ET",
    "FI",
    "FJ",
    "FK",
    "FM",
    "FO",
    "FR",
    "GA",
    "GB",
    "GD",
    "GE",
    "GF",
    "GG",
    "GH",
    "GI",
    "GL",
    "GM",
    "GN",
    "GP",
    "GQ",
    "GR",
    "GS",
    "GT",
    "GU",
    "GW",
    "GY",
    "HK",
    "HM",
    "HN",
    "HR",
    "HT",
    "HU",
    "ID",
    "IE",
    "IL",
    "IM",
    "IN",
    "IO",
    "IQ",
    "IR",
    "IS",
    "IT",
    "JE",
    "JM",
    "JO",
    "JP",
    "KE",
    "KG",
    "KH",
    "KI",
    "KM",
    "KN",
    "KP",
    "KR",
    "KW",
    "KY",
    "KZ",
    "LA",
    "LB",
    "LC",
    "LI",
    "LK",
    "LR",
    "LS",
    "LT",
    "LU",
    "LV",
    "LY",
    "MA",
    "MC",
    "MD",
    "ME",
    "MF",
    "MG",
    "MH",
    "MK",
    "ML",
    "MM",
    "MN",
    "MO",
    "MP",
    "MQ",
    "MR",
    "MS",
    "MT",
    "MU",
    "MV",
    "MW",
    "MX",
    "MY",
    "MZ",
    "NA",
    "NC",
    "NE",
    "NF",
    "NG",
    "NI",
    "NL",
    "NO",
    "NP",
    "NR",
    "NU",
    "NZ",
    "OM",
    "PA",
    "PE",
    "PF",
    "PG",
    "PH",
    "PK",
    "PL",
    "PM",
    "PN",
    "PR",
    "PS",
    "PT",
    "PW",
    "PY",
    "QA",
    "RE",
    "RO",
    "RS",
    "RU",
    "RW",
    "SA",
    "SB",
    "SC",
    "SD",
    "SE",
    "SG",
    "SH",
    "SI",
    "SJ",
    "SK",
    "SL",
    "SM",
    "SN",
    "SO",
    "SR",
    "SS",
    "ST",
    "SV",
    "SX",
    "SY",
    "SZ",
    "TC",
    "TD",
    "TF",
    "TG",
    "TH",
    "TJ",
    "TK",
    "TL",
    "TM",
    "TN",
    "TO",
    "TR",
    "TT",
    "TV",
    "TW",
    "TZ",
    "UA",
    "UG",
    "UM",
    "US",
    "UY",
    "UZ",
    "VA",
    "VC",
    "VE",
    "VG",
    "VI",
    "VN",
    "VU",
    "WF",
    "WS",
    "YE",
    "YT",
    "ZA",
    "ZM",
    "ZW",
    # Also include EU as it's commonly used
    "EU",
}


@deconstructible
class ISO3166Alpha2Validator:
    """Validate ISO 3166-1 alpha-2 country codes."""

    message = _(
        "Enter a valid ISO 3166-1 alpha-2 country code (e.g., 'US', 'DE', 'EE')."
    )
    code = "invalid_country_code"

    def __init__(self, message=None, code=None):
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code

    def __call__(self, value):
        if value and value.upper() not in ISO_3166_1_ALPHA_2_CODES:
            raise ValidationError(self.message, code=self.code)


validate_iso_3166_alpha2 = ISO3166Alpha2Validator()


VALID_PERSONAL_TITLES = {"Mr", "Ms", "Mrs", "Miss", "Dr", "Prof", "Sir", "Dame"}


def validate_personal_title(value):
    """Validate personal title against a set of allowed values."""
    if not value:
        return
    if value not in VALID_PERSONAL_TITLES:
        raise ValidationError(
            _("Invalid personal title '%(value)s'. Allowed values are: %(allowed)s."),
            params={
                "value": value,
                "allowed": ", ".join(sorted(VALID_PERSONAL_TITLES)),
            },
        )


def validate_gender(value):
    if not value:
        return
    valid_values = {key for key, _ in GENDER_CHOICES}
    if value not in valid_values:
        raise ValidationError(
            _("Invalid gender '%(value)s'. Allowed values are: %(allowed)s."),
            params={
                "value": value,
                "allowed": ", ".join(sorted(valid_values)),
            },
        )


def validate_nationalities(value):
    """Validate that nationalities is a list of valid ISO 3166-1 alpha-2 codes."""
    if not value:
        return
    if not isinstance(value, list):
        raise ValidationError(_("Nationalities must be a list."))
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(_("Each nationality must be a string."))
        if item.upper() not in ISO_3166_1_ALPHA_2_CODES:
            raise ValidationError(
                _("'%(value)s' is not a valid ISO 3166-1 alpha-2 country code."),
                params={"value": item},
            )


def validate_schac_organization_type(value):
    """
    Validate SCHAC homeOrganizationType URN format.

    SCHAC URN format: urn:schac:homeOrganizationType:<country>:<type>
    Examples:
    - urn:schac:homeOrganizationType:int:university
    - urn:schac:homeOrganizationType:de:research-institution
    """
    if not value:
        return

    if not isinstance(value, str):
        raise ValidationError(_("Organization type must be a string."))

    # SCHAC URN pattern
    schac_pattern = re.compile(
        r"^urn:schac:homeOrganizationType:[a-z]{2,3}:[a-zA-Z0-9\-]+$"
    )

    # Also accept simple organization types without URN prefix
    simple_pattern = re.compile(r"^[a-zA-Z0-9\-_]+$")

    if not schac_pattern.match(value) and not simple_pattern.match(value):
        raise ValidationError(
            _(
                "Invalid organization type format. Use SCHAC URN format "
                "(e.g., 'urn:schac:homeOrganizationType:int:university') "
                "or a simple identifier (e.g., 'university')."
            )
        )


def validate_refeds_assurance_list(value):
    """
    Validate REFEDS Assurance Framework URIs.

    REFEDS assurance URIs are typically in the format:
    - https://refeds.org/assurance/IAP/...
    - https://refeds.org/assurance/ID/...
    - https://refeds.org/assurance/ATP/...
    - urn:oasis:names:tc:SAML:2.0:ac:classes:...
    """
    if not value:
        return

    if not isinstance(value, list):
        raise ValidationError(_("Assurance levels must be a list."))

    # URI patterns for assurance levels
    valid_patterns = [
        re.compile(r"^https://refeds\.org/assurance/"),
        re.compile(r"^urn:oasis:names:tc:SAML:"),
        re.compile(r"^https://"),  # Allow other HTTPS URIs
        re.compile(r"^urn:"),  # Allow other URNs
    ]

    invalid_items = []
    for item in value:
        if not isinstance(item, str):
            invalid_items.append(str(item))
            continue

        if not any(pattern.match(item) for pattern in valid_patterns):
            invalid_items.append(item)

    if invalid_items:
        raise ValidationError(
            _(
                "Invalid assurance URIs: %(items)s. "
                "Expected REFEDS assurance URIs or valid URNs."
            ),
            params={"items": ", ".join(invalid_items)},
        )


def validate_unix_path(path):
    """Validate that the given path is a valid Unix/Linux file path."""
    if not isinstance(path, str):
        raise ValidationError(_("Path must be a string."))

    if not path.strip():
        raise ValidationError(_("Path cannot be empty."))

    # Check for invalid characters in Unix paths
    NULL_CHAR = "\0"  # Null character is not allowed in Unix paths
    invalid_chars = [NULL_CHAR]
    if any(char in path for char in invalid_chars):
        raise ValidationError(_("Path contains invalid characters."))

    # Path should start with / for absolute paths (recommended for config files)
    if not path.startswith("/"):
        raise ValidationError(_("Path should be absolute (start with /)."))

    # Check path length (most Unix systems have limits)
    UNIX_PATH_MAX = 4096  # PATH_MAX on most Unix systems
    if len(path) > UNIX_PATH_MAX:
        raise ValidationError(_("Path is too long (maximum 4096 characters)."))

    # Check for path traversal attempts
    if "/../" in path or path.endswith("/..") or ".." in path.split():
        raise ValidationError(_("Path contains invalid directory traversal."))

    # Check each path component length (NAME_MAX is typically 255)
    UNIX_NAME_MAX = 255
    path_components = path.split("/")
    for component in path_components:
        if len(component) > UNIX_NAME_MAX:
            raise ValidationError(
                _("Path component is too long (maximum 255 characters).")
            )


# Patterns that indicate potential ReDoS vulnerability
_REDOS_PATTERNS = [
    r"\(\?P?<[^>]*>[^)]*[+*][^)]*\)[+*]",  # Nested quantifiers: (a+)+
    r"\([^)]*\|[^)]*\)[+*]{2,}",  # Overlapping alternations with quantifiers
    r"[+*]\?[+*]",  # Adjacent quantifiers
]
_REDOS_REGEX = re.compile("|".join(_REDOS_PATTERNS))
_MAX_REGEX_PATTERN_LENGTH = 200


def is_potentially_dangerous_regex(pattern: str) -> bool:
    """Check if a regex pattern might cause ReDoS.

    Returns True if the pattern exceeds the maximum length or contains
    constructs known to cause catastrophic backtracking.
    """
    if len(pattern) > _MAX_REGEX_PATTERN_LENGTH:
        return True
    return bool(_REDOS_REGEX.search(pattern))
