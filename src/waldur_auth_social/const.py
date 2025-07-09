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
            "organization": "affiliation org",
        },
        "extra_fields": "site_username",
    },
}
