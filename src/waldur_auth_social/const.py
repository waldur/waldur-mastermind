class ProviderChoices:
    TARA = "tara"
    EDUTEAMS = "eduteams"
    REMOTE_EDUTEAMS = "remote-eduteams"
    KEYCLOAK = "keycloak"

    CHOICES = (TARA, EDUTEAMS, KEYCLOAK)


WRITABLE_USER_FIELDS = (
    "first_name",
    "last_name",
    "identity_source",
    "organization",
    "affiliations",
    "civil_number",
    "email",
    "phone_number",
    "birth_date",
    "gender",
    "personal_title",
    "place_of_birth",
    "address",
    "country_of_residence",
    "nationality",
    "nationalities",
    "organization_country",
    "organization_type",
    "organization_registry_code",
    "organization_vat_code",
    "organization_address",
    "eduperson_assurance",
)

SECRET_PROVIDER_FIELDS = (
    "client_secret",
    "user_field",
    "user_claim",
    "attribute_mapping",
    "extra_fields",
)

PROVIDER_DEFAULTS = {
    ProviderChoices.TARA: {
        "user_field": "civil_number",
        "user_claim": "sub",
        "attribute_mapping": {
            "first_name": "given_name",
            "last_name": "family_name",
            "civil_number": "sub",
        },
        "extra_fields": "amr profile_attributes_translit",
    },
    ProviderChoices.EDUTEAMS: {
        "user_field": "username",
        "user_claim": "sub",
        "attribute_mapping": {
            "first_name": "given_name",
            "last_name": "family_name",
            "affiliations": "voperson_external_affiliation",
            "email": "email",
        },
        "extra_fields": "eduperson_assurance",
        "extra_scope": "profile email eduperson_assurance ssh_public_key",
    },
    ProviderChoices.REMOTE_EDUTEAMS: {
        "user_field": "username",
        "user_claim": "voperson_id",
        "attribute_mapping": {
            "first_name": "given_name",
            "last_name": "family_name",
            "affiliations": "voperson_external_affiliation",
            "email": "mail",
        },
        "extra_fields": "eduperson_assurance",
        "extra_scope": "profile email eduperson_assurance ssh_public_key",
    },
    ProviderChoices.KEYCLOAK: {
        "user_field": "username",
        "user_claim": "sub",
        "attribute_mapping": {
            "email": "email",
            "first_name": "given_name",
            "last_name": "family_name",
            "identity_source": "identity_source",
            "organization": "schac_home_organization affiliation org",
            "civil_number": "schacPersonalUniqueID",
            # User profile attributes from OIDC claims
            "gender": "gender",
            "birth_date": "birthdate",
            "personal_title": "schacPersonalTitle",
            "place_of_birth": "schacPlaceOfBirth",
            "address": "address",
            "country_of_residence": "schacCountryOfResidence",
            "nationality": "schacCountryOfCitizenship",
            "organization_country": "org_country",
            "organization_type": "schacHomeOrganizationType",
            "organization_registry_code": "organization_registry_code",
            "organization_vat_code": "organization_vat_code",
            "organization_address": "organization_address",
            "eduperson_assurance": "eduperson_assurance",
            "phone_number": "phone_number",
        },
        "extra_fields": "site_username",
    },
}
