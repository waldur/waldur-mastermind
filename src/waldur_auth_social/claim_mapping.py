"""
OIDC claim mapping suggestions for Waldur User model fields.

This module provides logic to suggest OIDC claims that can be mapped to Waldur
User model fields, based on:
1. Existing PROVIDER_DEFAULTS mappings
2. Standard OIDC claim names
3. Common SAML/eduPerson attribute naming conventions
"""

from waldur_auth_social.const import PROVIDER_DEFAULTS, WRITABLE_USER_FIELDS

# Human-readable descriptions for Waldur User fields
FIELD_DESCRIPTIONS = {
    "first_name": "User's first/given name",
    "last_name": "User's last/family name",
    "email": "User's email address",
    "organization": "User's organization/institution name",
    "affiliations": "User's organizational affiliations",
    "civil_number": "National identity number or unique personal identifier",
    "phone_number": "User's phone number",
    "identity_source": "Source of identity (e.g., institution that authenticated the user)",
    "birth_date": "User's date of birth",
    "gender": "User's gender",
    "personal_title": "User's personal title (e.g., Mr., Dr.)",
    "place_of_birth": "User's place of birth",
    "country_of_residence": "User's country of residence",
    "nationality": "User's nationality/citizenship (single value)",
    "nationalities": "User's nationalities/citizenships (multiple values)",
    "organization_country": "Country of the user's organization",
    "organization_type": "Type of the user's organization",
    "eduperson_assurance": "Level of identity assurance",
}

# Standard OIDC claims and common alternatives for each Waldur field
# Order matters - more common/standard claims come first
STANDARD_CLAIM_SUGGESTIONS = {
    "first_name": ["given_name", "first_name", "firstName", "givenName"],
    "last_name": [
        "family_name",
        "last_name",
        "lastName",
        "familyName",
        "surname",
        "sn",
    ],
    "email": ["email", "mail", "emailAddress", "email_address"],
    "organization": [
        "schac_home_organization",
        "schacHomeOrganization",
        "org",
        "organization",
        "affiliation",
        "o",
        "institution",
        "company",
    ],
    "affiliations": [
        "voperson_external_affiliation",
        "voPersonExternalAffiliation",
        "eduperson_scoped_affiliation",
        "eduPersonScopedAffiliation",
        "affiliation",
        "affiliations",
    ],
    "civil_number": [
        "sub",
        "schacPersonalUniqueID",
        "schac_personal_unique_id",
        "personal_unique_id",
        "national_id",
        "civil_number",
        "personalIdentifier",
    ],
    "phone_number": ["phone_number", "phone", "telephone", "telephoneNumber", "mobile"],
    "identity_source": [
        "identity_source",
        "identitySource",
        "idp",
        "identity_provider",
        "authenticating_authority",
    ],
    "birth_date": ["birthdate", "birth_date", "dateOfBirth", "date_of_birth"],
    "gender": ["gender", "sex"],
    "personal_title": [
        "schacPersonalTitle",
        "schac_personal_title",
        "personal_title",
        "title",
        "honorificPrefix",
    ],
    "place_of_birth": [
        "schacPlaceOfBirth",
        "schac_place_of_birth",
        "place_of_birth",
        "birthplace",
    ],
    "country_of_residence": [
        "schacCountryOfResidence",
        "schac_country_of_residence",
        "country_of_residence",
        "country",
        "address.country",
    ],
    "nationality": [
        "schacCountryOfCitizenship",
        "schac_country_of_citizenship",
        "nationality",
        "citizenship",
        "country_of_citizenship",
    ],
    "nationalities": [
        "schacCountryOfCitizenship",
        "nationalities",
        "citizenships",
    ],
    "organization_country": [
        "org_reg_country",
        "org_country",
        "organization_country",
        "schacHomeOrganizationCountry",
    ],
    "organization_type": [
        "schacHomeOrganizationType",
        "schac_home_organization_type",
        "organization_type",
        "org_type",
    ],
    "eduperson_assurance": [
        "eduperson_assurance",
        "eduPersonAssurance",
        "assurance",
        "acr",
        "amr",
    ],
}

# Claims that typically require specific scopes
CLAIM_TO_SCOPE = {
    "email": "email",
    "mail": "email",
    "given_name": "profile",
    "family_name": "profile",
    "name": "profile",
    "birthdate": "profile",
    "gender": "profile",
    "phone_number": "phone",
    "address": "address",
    "eduperson_assurance": "eduperson_assurance",
    "eduPersonAssurance": "eduperson_assurance",
    "voperson_external_affiliation": "voperson_external_affiliation",
    "ssh_public_key": "ssh_public_key",
}


def _extract_claims_from_provider_defaults():
    """
    Extract all claim names used in PROVIDER_DEFAULTS attribute_mapping.
    Returns a dict mapping waldur_field -> set of claim names.
    """
    field_to_claims = {}
    for provider_config in PROVIDER_DEFAULTS.values():
        mapping = provider_config.get("attribute_mapping", {})
        for waldur_field, claims_str in mapping.items():
            if waldur_field not in field_to_claims:
                field_to_claims[waldur_field] = set()
            # Claims can be space-separated (first match wins)
            for claim in claims_str.split():
                field_to_claims[waldur_field].add(claim.strip())
    return field_to_claims


def get_claim_suggestions_for_field(field: str) -> list[str]:
    """
    Get ordered list of suggested OIDC claims for a Waldur User field.

    Combines:
    1. Claims from PROVIDER_DEFAULTS (known working mappings)
    2. Standard OIDC claims
    3. Common alternative names

    Returns deduplicated list ordered by likelihood.
    """
    suggestions = []
    seen = set()

    # First: add claims from existing provider defaults (proven to work)
    defaults_claims = _extract_claims_from_provider_defaults()
    if field in defaults_claims:
        for claim in defaults_claims[field]:
            if claim not in seen:
                suggestions.append(claim)
                seen.add(claim)

    # Second: add standard OIDC claims
    if field in STANDARD_CLAIM_SUGGESTIONS:
        for claim in STANDARD_CLAIM_SUGGESTIONS[field]:
            if claim not in seen:
                suggestions.append(claim)
                seen.add(claim)

    return suggestions


def get_available_claims_for_field(field: str, idp_claims: list[str]) -> list[str]:
    """
    Get claims from the IdP that match suggestions for a Waldur field.

    Args:
        field: Waldur User model field name
        idp_claims: List of claims supported by the IdP

    Returns:
        List of IdP claims that could map to this field, ordered by priority
    """
    suggestions = get_claim_suggestions_for_field(field)
    idp_claims_lower = {c.lower(): c for c in idp_claims}

    available = []
    for suggestion in suggestions:
        # Exact match
        if suggestion in idp_claims:
            available.append(suggestion)
        # Case-insensitive match
        elif suggestion.lower() in idp_claims_lower:
            available.append(idp_claims_lower[suggestion.lower()])

    return available


def get_waldur_field_suggestions(idp_claims: list[str]) -> list[dict]:
    """
    Get all Waldur User fields with their claim mapping suggestions.

    Args:
        idp_claims: List of claims supported by the OIDC provider

    Returns:
        List of dicts with field info and suggested/available claims
    """
    result = []
    for field in WRITABLE_USER_FIELDS:
        suggested = get_claim_suggestions_for_field(field)
        available = get_available_claims_for_field(field, idp_claims)
        result.append(
            {
                "field": field,
                "description": FIELD_DESCRIPTIONS.get(field, field),
                "suggested_claims": suggested,
                "available_claims": available,
            }
        )
    return result


def get_suggested_scopes(idp_claims: list[str], idp_scopes: list[str]) -> list[str]:
    """
    Get recommended scopes based on claims that would be useful.

    Args:
        idp_claims: Claims supported by the IdP
        idp_scopes: Scopes supported by the IdP

    Returns:
        List of recommended scopes to request
    """
    # Always include openid
    recommended = {"openid"}

    # Add scopes for claims we might want
    for claim in idp_claims:
        if claim in CLAIM_TO_SCOPE:
            scope = CLAIM_TO_SCOPE[claim]
            if scope in idp_scopes:
                recommended.add(scope)

    # Always recommend profile and email if available
    for scope in ["profile", "email"]:
        if scope in idp_scopes:
            recommended.add(scope)

    return sorted(recommended)


def generate_default_mapping(idp_claims: list[str]) -> dict[str, str]:
    """
    Generate a default attribute_mapping based on available IdP claims.

    Args:
        idp_claims: Claims supported by the IdP

    Returns:
        Dict mapping Waldur fields to OIDC claims (space-separated if multiple)
    """
    mapping = {}
    for field in WRITABLE_USER_FIELDS:
        available = get_available_claims_for_field(field, idp_claims)
        if available:
            # Use space-separated claims (first match wins during auth)
            mapping[field] = " ".join(available)
    return mapping
