"""VAT number checks against the EU VIES service."""

import dataclasses
import logging
import re

import requests

logger = logging.getLogger(__name__)

EU_COUNTRIES = (
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR",
    "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI",
    "SK",
)  # fmt: skip

# EU services use the code EL for Greece instead of ISO GR.
EU_SERVICE_CODES = {"GR": "EL"}
ISO_CODES = {code: iso for iso, code in EU_SERVICE_CODES.items()}

VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"

# The number itself is malformed; anything else (including INVALID_REQUESTER_INFO,
# which is about requester details this client never sends) means VIES could not
# answer.
VIES_INVALID_ERRORS = {"INVALID_INPUT"}


class VIESUnavailable(Exception):
    pass


@dataclasses.dataclass
class VIESResult:
    valid: bool
    name: str = ""
    address: str = ""


def normalize_vat_code(vat_code: str) -> str:
    return re.sub(r"[\s.-]", "", vat_code or "").upper()


def get_vat_country(vat_code: str, country: str = "") -> str:
    """ISO country of a VAT number: its prefix, or the given country."""
    code = normalize_vat_code(vat_code)
    if re.match(r"^[A-Z]{2}", code):
        return ISO_CODES.get(code[:2], code[:2])
    return (country or "").upper()


def is_eu_vat_code(vat_code: str, country: str = "") -> bool:
    return bool(vat_code) and get_vat_country(vat_code, country) in EU_COUNTRIES


def _clean(value) -> str:
    value = (value or "").strip()
    return "" if value == "---" else value


def check_vat_number(vat_code: str, country: str = "") -> VIESResult:
    """Ask VIES whether an EU VAT number is registered. Raises VIESUnavailable."""
    code = normalize_vat_code(vat_code)
    iso = get_vat_country(code, country)
    service_code = EU_SERVICE_CODES.get(iso, iso)
    number = code[2:] if code[:2] in (iso, service_code) else code
    try:
        response = requests.post(
            VIES_URL,
            json={"countryCode": service_code, "vatNumber": number},
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise VIESUnavailable(f"VIES request failed: {e}") from e

    if "valid" in data:
        return VIESResult(
            valid=bool(data["valid"]),
            name=_clean(data.get("name")),
            address=_clean(data.get("address")),
        )
    errors = {error.get("error") for error in data.get("errorWrappers") or []}
    if errors & VIES_INVALID_ERRORS:
        return VIESResult(valid=False)
    raise VIESUnavailable(f"VIES could not check {iso}{number}: {errors or data}")
