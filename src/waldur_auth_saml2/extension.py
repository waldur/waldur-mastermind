import saml2

from waldur_core.core import WaldurExtension


class SAML2Extension(WaldurExtension):
    class Settings:
        # These shouldn't be configurable by user -- see SAML2 section for details
        SAML_CREATE_UNKNOWN_USER = True
        SAML_DJANGO_USER_MAIN_ATTRIBUTE = 'username'

    @staticmethod
    def update_settings(settings):
        settings['SAML_ATTRIBUTE_MAPPING'] = settings['WALDUR_AUTH_SAML2'][
            'SAML_ATTRIBUTE_MAPPING'
        ]

        settings['SAML_CONFIG'] = {
            # full path to the xmlsec1 binary program
            'xmlsec_binary': settings['WALDUR_AUTH_SAML2']['XMLSEC_BINARY'],
            # your entity id, usually your subdomain plus the url to the metadata view
            'entityid': settings['WALDUR_AUTH_SAML2']['BASE_URL']
            + '/api-auth/saml2/metadata/',
            'entity_category': settings['WALDUR_AUTH_SAML2']['CATEGORIES'],
            # directory with attribute mapping
            'attribute_map_dir': settings['WALDUR_AUTH_SAML2']['ATTRIBUTE_MAP_DIR'],
            # this block states what services we provide
            'service': {
                # we are just a lonely SP
                'sp': {
                    # Indicates if the entity will sign the logout requests
                    'logout_requests_signed': settings['WALDUR_AUTH_SAML2'][
                        'LOGOUT_REQUESTS_SIGNED'
                    ],
                    # Indicates if the authentication requests sent should be signed by default
                    'authn_requests_signed': settings['WALDUR_AUTH_SAML2'][
                        'AUTHN_REQUESTS_SIGNED'
                    ],
                    'endpoints': {
                        # url and binding to the assertion consumer service view
                        # do not change the binding or service name
                        'assertion_consumer_service': [
                            (
                                settings['WALDUR_AUTH_SAML2']['BASE_URL']
                                + '/api-auth/saml2/login/complete/',
                                saml2.BINDING_HTTP_POST,
                            ),
                        ],
                        # url and binding to the single logout service view
                        # do not change the binding or service name
                        'single_logout_service': [
                            (
                                settings['WALDUR_AUTH_SAML2']['BASE_URL']
                                + '/api-auth/saml2/logout/complete/',
                                saml2.BINDING_HTTP_REDIRECT,
                            ),
                            (
                                settings['WALDUR_AUTH_SAML2']['BASE_URL']
                                + '/api-auth/saml2/logout/complete/',
                                saml2.BINDING_HTTP_POST,
                            ),
                        ],
                    },
                    'extensions': {
                        'mdui': {
                            'UIInfo': {
                                'display_name': {
                                    'lang': 'en',
                                    'text': settings['WALDUR_AUTH_SAML2'][
                                        'DISPLAY_NAME'
                                    ],
                                },
                                'description': {
                                    'lang': 'en',
                                    'text': settings['WALDUR_AUTH_SAML2'][
                                        'DESCRIPTION'
                                    ],
                                },
                                'privacy_statement_url': {
                                    'lang': 'en',
                                    'text': settings['WALDUR_AUTH_SAML2'][
                                        'PRIVACY_STATEMENT_URL'
                                    ],
                                },
                            },
                        },
                        'mdrpi': {
                            'RegistrationInfo': {
                                'registration_policy': {
                                    'lang': 'en',
                                    'text': settings['WALDUR_AUTH_SAML2'][
                                        'REGISTRATION_POLICY'
                                    ],
                                },
                                'registrationAuthority': settings['WALDUR_AUTH_SAML2'][
                                    'REGISTRATION_AUTHORITY'
                                ],
                                'registrationInstant': settings['WALDUR_AUTH_SAML2'][
                                    'REGISTRATION_INSTANT'
                                ],
                            },
                        },
                    },
                    # attributes that this project needs to identify a user
                    'required_attributes': settings['WALDUR_AUTH_SAML2'][
                        'REQUIRED_ATTRIBUTES'
                    ],
                    # attributes that may be useful to have but not required
                    'optional_attributes': settings['WALDUR_AUTH_SAML2'][
                        'OPTIONAL_ATTRIBUTES'
                    ],
                },
            },
            # Use database metadata loader
            # See also: https://github.com/rohe/pysaml2/issues/216
            'metadata': [
                {
                    'class': 'waldur_auth_saml2.utils.DatabaseMetadataLoader',
                    'metadata': [('waldur_auth_saml2.utils.DatabaseMetadataLoader',)],
                },
            ],
            'organization': settings['WALDUR_AUTH_SAML2']['ORGANIZATION'],
            # set to 1 to output debugging information
            'debug': int(settings['WALDUR_AUTH_SAML2']['DEBUG']),
            # signing
            'key_file': settings['WALDUR_AUTH_SAML2']['KEY_FILE'],  # private part
            'cert_file': settings['WALDUR_AUTH_SAML2']['CERT_FILE'],  # public part
            'only_use_keys_in_metadata': False,
            'allow_unknown_attributes': True,
            'accepted_time_diff': 120,
        }
        settings['AUTHENTICATION_BACKENDS'] += (
            'waldur_auth_saml2.auth.WaldurSaml2Backend',
        )
        if settings['WALDUR_AUTH_SAML2']['LOG_FILE'] != '':
            level = settings['WALDUR_AUTH_SAML2']['LOG_LEVEL'].upper()
            settings['LOGGING']['handlers']['file-saml2'] = {
                'class': 'logging.handlers.WatchedFileHandler',
                'filename': settings['WALDUR_AUTH_SAML2']['LOG_FILE'],
                'formatter': 'simple',
                'level': level,
            }

            settings['LOGGING']['loggers']['djangosaml2'] = {
                'handlers': ['file-saml2'],
                'propagate': True,
                'level': level,
            }

            settings['LOGGING']['loggers']['saml2'] = {
                'handlers': ['file-saml2'],
                'propagate': True,
                'level': level,
            }

        # Use the same keypairs for both signing and encryption.
        # Otherwise pysaml2 doesn't decrypt encrypted assertion.
        # See also: https://github.com/knaperek/djangosaml2/issues/22
        settings['SAML_CONFIG']['encryption_keypairs'] = [
            {
                'key_file': settings['WALDUR_AUTH_SAML2']['KEY_FILE'],
                'cert_file': settings['WALDUR_AUTH_SAML2']['CERT_FILE'],
            }
        ]

        # Implement backward-compatible style for remote metadata specification.
        for remote in settings['WALDUR_AUTH_SAML2']['IDP_METADATA_REMOTE']:
            settings['SAML_CONFIG']['metadata'].append(
                {
                    'class': 'saml2.mdstore.MetaDataExtern',
                    'metadata': [(remote['url'], remote['cert'])],
                }
            )

    @staticmethod
    def django_app():
        return 'waldur_auth_saml2'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns
