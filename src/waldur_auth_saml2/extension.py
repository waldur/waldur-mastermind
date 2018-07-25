import datetime
import saml2

from saml2.entity_category.edugain import COC

from waldur_core.core import WaldurExtension


class SAML2Extension(WaldurExtension):

    class Settings:
        # Read more: https://github.com/rohe/pysaml2-3/blob/master/doc/howto/config.rst
        # For an example configuration refer to the packaging/etc/waldur/saml2.conf.py.example
        # wiki: https://opennode.atlassian.net/wiki/display/WD/Identity+providers
        WALDUR_AUTH_SAML2 = {
            # used for assigning the registration method to the user
            'name': 'saml2',
            # full path to the xmlsec1 binary program
            'xmlsec_binary': '/usr/bin/xmlsec1',
            # required for assertion consumer, single logout services and entity ID
            'base_url': '',
            # directory with attribute mapping
            'attribute_map_dir': '',
            # set to True to output debugging information
            'debug': False,
            # IdPs metadata XML files stored locally
            'idp_metadata_local': [],
            # IdPs metadata XML files stored remotely
            'idp_metadata_remote': [],
            # logging
            # empty to disable logging SAML2-related stuff to file
            'log_file': '',
            'log_level': 'INFO',
            # Indicates if the entity will sign the logout requests
            'logout_requests_signed': 'true',
            # Indicates if the authentication requests sent should be signed by default
            'authn_requests_signed': 'true',
            # Identifies the Signature algorithm URL according to the XML Signature specification
            # SHA1 is used by default
            'signature_algorithm': None,
            # Identifies the Message Digest algorithm URL according to the XML Signature specification
            # SHA1 is used by default
            'digest_algorithm': None,
            # Identified NameID format to use. None means default, empty string ("") disables addition of entity
            'nameid_format': None,
            # PEM formatted certificate chain file
            'cert_file': '',
            # PEM formatted certificate key file
            'key_file': '',
            # SAML attributes that are required to identify a user
            'required_attributes': [],
            # SAML attributes that may be useful to have but not required
            'optional_attributes': [],
            # mapping between SAML attributes and User fields
            'saml_attribute_mapping': {},
            # organization responsible for the service
            # you can set multilanguage information here
            'organization': {},

            # links to the entity categories
            'categories': [COC],

            # attributes required by CoC
            # https://wiki.refeds.org/display/CODE/SAML+2+Profile+for+the+Data+Protection+Code+of+Conduct
            'privacy_statement_url': 'http://example.com/#/privacy-policy/',
            'display_name': 'Service provider display name',
            'description': 'Service provider description',

            # mdpi attributes
            'registration_policy': 'http://example.com/#/registration-policy/',
            'registration_authority': 'http://example.com/#/registration-authority/',
            'registration_instant': datetime.datetime(2017, 1, 1).isoformat(),

            'ENABLE_SINGLE_LOGOUT': False,
            'ALLOW_TO_SELECT_IDENTITY_PROVIDER': True,
            'IDENTITY_PROVIDER_URL': None,
            'IDENTITY_PROVIDER_LABEL': None,
            'DEFAULT_BINDING': saml2.BINDING_HTTP_POST,
        }

        # These shouldn't be configurable by user -- see SAML2 section for details
        SAML_CREATE_UNKNOWN_USER = True
        SAML_DJANGO_USER_MAIN_ATTRIBUTE = 'username'

        SAML_ATTRIBUTE_MAPPING = WALDUR_AUTH_SAML2['saml_attribute_mapping']

        SAML_CONFIG = {
            # full path to the xmlsec1 binary program
            'xmlsec_binary': WALDUR_AUTH_SAML2['xmlsec_binary'],

            # your entity id, usually your subdomain plus the url to the metadata view
            'entityid': WALDUR_AUTH_SAML2['base_url'] + '/api-auth/saml2/metadata/',

            'entity_category': WALDUR_AUTH_SAML2['categories'],

            # directory with attribute mapping
            'attribute_map_dir': WALDUR_AUTH_SAML2['attribute_map_dir'],

            # this block states what services we provide
            'service': {
                # we are just a lonely SP
                'sp': {
                    # Indicates if the entity will sign the logout requests
                    'logout_requests_signed': WALDUR_AUTH_SAML2['logout_requests_signed'],
                    # Indicates if the authentication requests sent should be signed by default
                    'authn_requests_signed': WALDUR_AUTH_SAML2['authn_requests_signed'],

                    'endpoints': {
                        # url and binding to the assertion consumer service view
                        # do not change the binding or service name
                        'assertion_consumer_service': [
                            (WALDUR_AUTH_SAML2['base_url'] + '/api-auth/saml2/login/complete/',
                             saml2.BINDING_HTTP_POST),
                        ],
                        # url and binding to the single logout service view
                        # do not change the binding or service name
                        'single_logout_service': [
                            (WALDUR_AUTH_SAML2['base_url'] + '/api-auth/saml2/logout/complete/',
                             saml2.BINDING_HTTP_REDIRECT),
                            (WALDUR_AUTH_SAML2['base_url'] + '/api-auth/saml2/logout/complete/',
                             saml2.BINDING_HTTP_POST),
                        ],
                    },

                    'extensions': {
                        'mdui': {
                            'UIInfo': {
                                'display_name': {
                                    'lang': 'en',
                                    'text': WALDUR_AUTH_SAML2['display_name'],
                                },
                                'description': {
                                    'lang': 'en',
                                    'text': WALDUR_AUTH_SAML2['description'],
                                },
                                'privacy_statement_url': {
                                    'lang': 'en',
                                    'text': WALDUR_AUTH_SAML2['privacy_statement_url'],
                                },
                            },
                        },
                        'mdrpi': {
                            'RegistrationInfo': {
                                'registration_policy': {
                                    'lang': 'en',
                                    'text': WALDUR_AUTH_SAML2['registration_policy'],
                                },
                                'registrationAuthority': WALDUR_AUTH_SAML2['registration_authority'],
                                'registrationInstant': WALDUR_AUTH_SAML2['registration_instant'],
                            },
                        }
                    },

                    # attributes that this project needs to identify a user
                    'required_attributes': WALDUR_AUTH_SAML2['required_attributes'],

                    # attributes that may be useful to have but not required
                    'optional_attributes': WALDUR_AUTH_SAML2['optional_attributes'],
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

            'organization': WALDUR_AUTH_SAML2['organization'],

            # set to 1 to output debugging information
            'debug': int(WALDUR_AUTH_SAML2['debug']),

            # signing
            'key_file': WALDUR_AUTH_SAML2['key_file'],  # private part
            'cert_file': WALDUR_AUTH_SAML2['cert_file'],  # public part

            'only_use_keys_in_metadata': False,
            'allow_unknown_attributes': True,

            'accepted_time_diff': 120,
        }

    @staticmethod
    def update_settings(settings):
        settings['AUTHENTICATION_BACKENDS'] += ('djangosaml2.backends.Saml2Backend',)
        if settings['WALDUR_AUTH_SAML2']['log_file'] != '':
            level = settings['WALDUR_AUTH_SAML2']['log_level'].upper()
            settings['LOGGING']['handlers']['file-saml2'] = {
                'class': 'logging.handlers.WatchedFileHandler',
                'filename': settings['WALDUR_AUTH_SAML2']['log_file'],
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
        settings['SAML_CONFIG']['encryption_keypairs'] = [{
            'key_file': settings['WALDUR_AUTH_SAML2']['key_file'],
            'cert_file': settings['WALDUR_AUTH_SAML2']['cert_file'],
        }]

        # Implement backward-compatible style for remote metadata specification.
        for remote in settings['WALDUR_AUTH_SAML2']['idp_metadata_remote']:
            settings['SAML_CONFIG']['metadata'].append({
                'class': 'saml2.mdstore.MetaDataExtern',
                'metadata': [(remote['url'], remote['cert'])]
            })

    @staticmethod
    def get_public_settings():
        return [
            'ENABLE_SINGLE_LOGOUT',
            'ALLOW_TO_SELECT_IDENTITY_PROVIDER',
            'IDENTITY_PROVIDER_URL',
            'IDENTITY_PROVIDER_LABEL',
        ]

    @staticmethod
    def django_app():
        return 'waldur_auth_saml2'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns

    @staticmethod
    def celery_tasks():
        from datetime import timedelta
        return {
            'waldur-auth-saml2-sync-providers': {
                'task': 'waldur_auth_saml2.sync_providers',
                'schedule': timedelta(minutes=60),
                'args': (),
            },
        }
