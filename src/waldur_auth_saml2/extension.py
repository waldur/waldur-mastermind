import copy

import saml2

from waldur_core.core import WaldurExtension

_MISSING = object()


def saml_config_changes(before, after):
    """The parts of SAML_CONFIG that a deployment's saml2.conf.py set or changed."""
    changes = {}
    for key, value in after.items():
        previous = before.get(key, _MISSING)
        if isinstance(value, dict) and isinstance(previous, dict):
            nested = saml_config_changes(previous, value)
            if nested:
                changes[key] = nested
        elif value != previous:
            changes[key] = value
    return changes


def _merge(config, changes):
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            _merge(config[key], value)
        else:
            config[key] = value


class SAML2Extension(WaldurExtension):
    class Settings:
        # These shouldn't be configurable by user -- see SAML2 section for details
        SAML_CREATE_UNKNOWN_USER = True
        SAML_DJANGO_USER_MAIN_ATTRIBUTE = "username"

    @staticmethod
    def update_settings(settings, saml_config_overrides=None):
        """Derive SAML_CONFIG from WALDUR_AUTH_SAML2 and WALDUR_CORE.

        ``saml_config_overrides`` are what a deployment's saml2.conf.py changed
        in SAML_CONFIG; they are applied on top of the derived configuration.
        """
        settings["SAML_ATTRIBUTE_MAPPING"] = settings["WALDUR_AUTH_SAML2"][
            "SAML_ATTRIBUTE_MAPPING"
        ]

        settings["SAML_CONFIG"] = {
            # full path to the xmlsec1 binary program
            "xmlsec_binary": settings["WALDUR_AUTH_SAML2"]["XMLSEC_BINARY"],
            # your entity id, usually your subdomain plus the url to the metadata view
            "entityid": settings["WALDUR_CORE"]["MASTERMIND_URL"]
            + "/api-auth/saml2/metadata/",
            "entity_category": settings["WALDUR_AUTH_SAML2"]["CATEGORIES"],
            # directory with attribute mapping
            "attribute_map_dir": settings["WALDUR_AUTH_SAML2"]["ATTRIBUTE_MAP_DIR"],
            # this block states what services we provide
            "service": {
                # we are just a lonely SP
                "sp": {
                    # Indicates if the entity will sign the logout requests
                    "logout_requests_signed": settings["WALDUR_AUTH_SAML2"][
                        "LOGOUT_REQUESTS_SIGNED"
                    ],
                    # Indicates if the authentication requests sent should be signed by default
                    "authn_requests_signed": settings["WALDUR_AUTH_SAML2"][
                        "AUTHN_REQUESTS_SIGNED"
                    ],
                    # The browser does not send the SameSite=Lax session cookie
                    # with the identity provider's cross-site POST, so the
                    # request a response answers is not found in the session.
                    "allow_unsolicited": settings["WALDUR_AUTH_SAML2"][
                        "ALLOW_UNSOLICITED"
                    ],
                    "endpoints": {
                        # url and binding to the assertion consumer service view
                        # do not change the binding or service name
                        "assertion_consumer_service": [
                            (
                                settings["WALDUR_CORE"]["MASTERMIND_URL"]
                                + "/api-auth/saml2/login/complete/",
                                saml2.BINDING_HTTP_POST,
                            ),
                        ],
                        # url and binding to the single logout service view
                        # do not change the binding or service name
                        "single_logout_service": [
                            (
                                settings["WALDUR_CORE"]["MASTERMIND_URL"]
                                + "/api-auth/saml2/logout/complete/",
                                saml2.BINDING_HTTP_REDIRECT,
                            ),
                            (
                                settings["WALDUR_CORE"]["MASTERMIND_URL"]
                                + "/api-auth/saml2/logout/complete/",
                                saml2.BINDING_HTTP_POST,
                            ),
                        ],
                    },
                    "extensions": {
                        "mdui": {
                            "UIInfo": {
                                "display_name": {
                                    "lang": "en",
                                    "text": settings["WALDUR_AUTH_SAML2"][
                                        "DISPLAY_NAME"
                                    ],
                                },
                                "description": {
                                    "lang": "en",
                                    "text": settings["WALDUR_AUTH_SAML2"][
                                        "DESCRIPTION"
                                    ],
                                },
                                "privacy_statement_url": {
                                    "lang": "en",
                                    "text": settings["WALDUR_AUTH_SAML2"][
                                        "PRIVACY_STATEMENT_URL"
                                    ],
                                },
                            },
                        },
                        "mdrpi": {
                            "RegistrationInfo": {
                                "registration_policy": {
                                    "lang": "en",
                                    "text": settings["WALDUR_AUTH_SAML2"][
                                        "REGISTRATION_POLICY"
                                    ],
                                },
                                "registrationAuthority": settings["WALDUR_AUTH_SAML2"][
                                    "REGISTRATION_AUTHORITY"
                                ],
                                "registrationInstant": settings["WALDUR_AUTH_SAML2"][
                                    "REGISTRATION_INSTANT"
                                ],
                            },
                        },
                    },
                    # attributes that this project needs to identify a user
                    "required_attributes": settings["WALDUR_AUTH_SAML2"][
                        "REQUIRED_ATTRIBUTES"
                    ],
                    # attributes that may be useful to have but not required
                    "optional_attributes": settings["WALDUR_AUTH_SAML2"][
                        "OPTIONAL_ATTRIBUTES"
                    ],
                },
            },
            # Use database metadata loader
            # See also: https://github.com/rohe/pysaml2/issues/216
            "metadata": [
                {
                    "class": "waldur_auth_saml2.utils.DatabaseMetadataLoader",
                    "metadata": [("waldur_auth_saml2.utils.DatabaseMetadataLoader",)],
                },
            ],
            "organization": settings["WALDUR_AUTH_SAML2"]["ORGANIZATION"],
            # set to 1 to output debugging information
            "debug": int(settings["WALDUR_AUTH_SAML2"]["DEBUG"]),
            # signing
            "key_file": settings["WALDUR_AUTH_SAML2"]["KEY_FILE"],  # private part
            "cert_file": settings["WALDUR_AUTH_SAML2"]["CERT_FILE"],  # public part
            "allow_unknown_attributes": True,
            "accepted_time_diff": 120,
        }
        backend = "djangosaml2.backends.Saml2Backend"
        if backend not in settings["AUTHENTICATION_BACKENDS"]:
            settings["AUTHENTICATION_BACKENDS"] += (backend,)
        if settings["WALDUR_AUTH_SAML2"]["LOG_FILE"] != "":
            level = settings["WALDUR_AUTH_SAML2"]["LOG_LEVEL"].upper()
            settings["LOGGING"]["handlers"]["file-saml2"] = {
                "class": "logging.handlers.WatchedFileHandler",
                "filename": settings["WALDUR_AUTH_SAML2"]["LOG_FILE"],
                "formatter": "simple",
                "level": level,
            }

            settings["LOGGING"]["loggers"]["djangosaml2"] = {
                "handlers": ["file-saml2"],
                "propagate": True,
                "level": level,
            }

            settings["LOGGING"]["loggers"]["saml2"] = {
                "handlers": ["file-saml2"],
                "propagate": True,
                "level": level,
            }

        # Use the same keypairs for both signing and encryption.
        # Otherwise pysaml2 doesn't decrypt encrypted assertion.
        # See also: https://github.com/knaperek/djangosaml2/issues/22
        settings["SAML_CONFIG"]["encryption_keypairs"] = [
            {
                "key_file": settings["WALDUR_AUTH_SAML2"]["KEY_FILE"],
                "cert_file": settings["WALDUR_AUTH_SAML2"]["CERT_FILE"],
            }
        ]

        # Implement backward-compatible style for remote metadata specification.
        for remote in settings["WALDUR_AUTH_SAML2"]["IDP_METADATA_REMOTE"]:
            settings["SAML_CONFIG"]["metadata"].append(
                {
                    "class": "saml2.mdstore.MetaDataExtern",
                    "metadata": [(remote["url"], remote["cert"])],
                }
            )

        if saml_config_overrides:
            overrides = copy.deepcopy(saml_config_overrides)
            # Waldur's views serve these URLs and bindings; they follow
            # MASTERMIND_URL and are not taken from configuration files.
            overrides.get("service", {}).get("sp", {}).pop("endpoints", None)
            _merge(settings["SAML_CONFIG"], overrides)

        # Applied last, so that no configuration file can weaken them:
        # verify signatures only with keys published in identity provider
        # metadata, never with the certificate a response carries itself, and
        # reject a response in which neither the response nor the assertion
        # is signed, even if want_response_signed is off.
        settings["SAML_CONFIG"]["only_use_keys_in_metadata"] = True
        settings["SAML_CONFIG"]["service"]["sp"][
            "want_assertions_or_response_signed"
        ] = True

    @staticmethod
    def django_app():
        return "waldur_auth_saml2"

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns
