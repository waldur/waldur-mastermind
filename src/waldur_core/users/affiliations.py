"""
Parser for SCHAC / MACE / eduPerson affiliation URN strings.

Used by the marketplace stats endpoints to expose parsed
organization / country / category fields server-side so the affiliation
details table can be paginated, filtered and ordered on the server.

A direct port of the homeport parser in
`waldur-homeport/src/reporting/users/affiliationParser.ts` — without the
i18n label dictionaries, which stay on the frontend.

Reference:
    https://incommon.org/community/mace-registries/mace-urn-registry/
    https://wiki.refeds.org/display/STAN/SCHAC
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORY_HOME_ORGANIZATION = "home-organization"
CATEGORY_PERSONAL_IDENTIFIER = "personal-identifier"
CATEGORY_ORGANIZATION_TYPE = "organization-type"
CATEGORY_USER_STATUS = "user-status"
CATEGORY_EDUPERSON = "eduperson"
CATEGORY_OTHER = "other"

CATEGORIES = {
    CATEGORY_HOME_ORGANIZATION,
    CATEGORY_PERSONAL_IDENTIFIER,
    CATEGORY_ORGANIZATION_TYPE,
    CATEGORY_USER_STATUS,
    CATEGORY_EDUPERSON,
    CATEGORY_OTHER,
}

KNOWN_COUNTRY_CODES = {
    "at",
    "be",
    "bg",
    "ch",
    "cy",
    "cz",
    "de",
    "dk",
    "ee",
    "es",
    "fi",
    "fr",
    "gr",
    "hr",
    "hu",
    "ie",
    "is",
    "it",
    "li",
    "lt",
    "lu",
    "lv",
    "mt",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "se",
    "si",
    "sk",
    "uk",
    "gb",
    "eu",
    "int",
    "us",
    "ca",
    "mx",
    "br",
    "ar",
    "cl",
    "co",
    "au",
    "nz",
    "jp",
    "cn",
    "kr",
    "tw",
    "sg",
    "hk",
    "in",
    "il",
    "za",
    "ae",
}

GENERIC_TLDS = {"com", "org", "net", "edu", "gov", "mil", "int"}

EDUPERSON_AFFILIATION_VALUES = {
    "faculty",
    "staff",
    "student",
    "employee",
    "alum",
    "member",
    "affiliate",
    "library-walk-in",
}


@dataclass(frozen=True)
class ParsedAffiliation:
    raw: str
    organization: str | None
    country: str | None
    category: str
    identifier: str | None


def _extract_country_from_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    parts = domain.lower().split(".")
    tld = parts[-1]
    if len(tld) == 2 and tld in KNOWN_COUNTRY_CODES:
        return tld
    if tld in GENERIC_TLDS:
        return None
    return tld if len(tld) == 2 else None


def _categorize(attribute: str | None) -> str:
    if not attribute:
        return CATEGORY_OTHER
    lower = attribute.lower()
    if "homeorganization" in lower and "type" not in lower:
        return CATEGORY_HOME_ORGANIZATION
    if "homeorganizationtype" in lower:
        return CATEGORY_ORGANIZATION_TYPE
    if "personalunique" in lower or "targetedid" in lower or "principalname" in lower:
        return CATEGORY_PERSONAL_IDENTIFIER
    if "userstatus" in lower:
        return CATEGORY_USER_STATUS
    if "eduperson" in lower or "affiliation" in lower:
        return CATEGORY_EDUPERSON
    return CATEGORY_OTHER


def _parse_mace_or_schac(
    parts: list[str],
    attribute_index: int,
) -> tuple[str | None, str | None, str | None]:
    """Return (organization, country, identifier) from the value parts."""
    if len(parts) <= attribute_index:
        return None, None, None
    attribute = parts[attribute_index]
    value_parts = parts[attribute_index + 1 :]
    if not value_parts:
        return None, None, None

    lower_attr = attribute.lower()

    if lower_attr == "homeorganization":
        domain = value_parts[0]
        return domain, _extract_country_from_domain(domain), None

    if lower_attr in ("personaluniquecode", "personaluniqueid"):
        if len(value_parts) >= 4:
            scope = value_parts[0]
            organization = value_parts[2]
            identifier = ":".join(value_parts[3:])
            country = (
                _extract_country_from_domain(organization) if scope == "int" else scope
            )
            return organization, country, identifier
        if len(value_parts) >= 3:
            scope = value_parts[0]
            identifier = value_parts[2]
            return None, scope, identifier

    if lower_attr == "homeorganizationtype":
        if len(value_parts) >= 2:
            scope = value_parts[0]
            return None, scope, None

    if lower_attr == "userstatus":
        if len(value_parts) >= 2:
            scope = value_parts[0]
            return None, scope, None

    return None, None, None


def parse_affiliation(value: str | None) -> ParsedAffiliation:
    """Return parsed organization / country / category / identifier for an affiliation URN.

    Unknown formats are returned with category="other" and best-effort
    organization extraction for bare domain-like strings.
    """
    raw = value or ""
    if not value or not isinstance(value, str):
        return ParsedAffiliation(raw, None, None, CATEGORY_OTHER, None)

    trimmed = value.strip()
    lower = trimmed.lower()

    organization: str | None = None
    country: str | None = None
    identifier: str | None = None
    attribute: str | None = None

    if lower.startswith("urn:mace:"):
        parts = trimmed.split(":")
        # urn:mace:authority:namespace:attribute:value...
        if len(parts) >= 5:
            attribute = parts[4]
            organization, country, identifier = _parse_mace_or_schac(parts, 4)
    elif lower.startswith("urn:schac:"):
        parts = trimmed.split(":")
        # urn:schac:attribute:scope:type:value...
        if len(parts) >= 3:
            attribute = parts[2]
            organization, country, identifier = _parse_mace_or_schac(parts, 2)
    elif lower.startswith("urn:"):
        # Generic URN — no parsed structure, just category=other.
        pass
    elif lower in EDUPERSON_AFFILIATION_VALUES:
        attribute = "affiliation"
    elif "@" in trimmed:
        _, scope = trimmed.split("@", 1)
        organization = scope
        country = _extract_country_from_domain(scope)
        attribute = "affiliation"
    elif "." in trimmed and " " not in trimmed:
        organization = trimmed
        country = _extract_country_from_domain(trimmed)

    return ParsedAffiliation(
        raw=raw,
        organization=organization,
        country=country,
        category=_categorize(attribute),
        identifier=identifier,
    )
