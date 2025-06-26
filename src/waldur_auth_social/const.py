class ProviderChoices:
    TARA = "tara"
    EDUTEAMS = "eduteams"
    KEYCLOAK = "keycloak"

    CHOICES = (TARA, EDUTEAMS, KEYCLOAK)


ALLOWED_FIELDS = (
    "first_name",
    "last_name",
    "identity_source",
    "organization",
    "affiliations",
    "civil_number",
    "email",
)

PROVIDER_DEFAULTS = {
    ProviderChoices.TARA: {
        "user_field": "civil_number",
        "attribute_mapping": {
            "first_name": "given_name",
            "last_name": "family_name",
            "civil_number": "sub",
        },
        "extra_fields": "amr profile_attributes_translit",
    },
    ProviderChoices.EDUTEAMS: {
        "user_claim": "sub voperson_id",
        "attribute_mapping": {
            "first_name": "given_name",
            "last_name": "family_name",
            "affiliations": "voperson_external_affiliation",
            "email": "mail",
        },
        "extra_fields": "eduperson_assurance",
    },
    ProviderChoices.KEYCLOAK: {
        "attribute_mapping": {
            "email": "email",
            "first_name": "given_name",
            "last_name": "family_name",
            "identity_source": "identity_source",
            "organization": "affiliation org",
        },
        "extra_fields": "site_username",
    },
}
