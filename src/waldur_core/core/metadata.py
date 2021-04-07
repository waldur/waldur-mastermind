from datetime import timedelta
from typing import List, Optional

from pydantic import BaseModel, Field


class WaldurCore(BaseModel):
    EXTENSIONS_AUTOREGISTER = Field(
        True,
        description='Defines whether extensions should be automatically registered.',
    )
    TOKEN_KEY = Field('x-auth-token', description='Header for token authentication.')
    AUTHENTICATION_METHODS = Field(
        ['LOCAL_SIGNIN'], description='List of enabled authentication methods.'
    )
    INVITATIONS_ENABLED = Field(
        True, description='Allows to disable invitations feature.'
    )
    ALLOW_SIGNUP_WITHOUT_INVITATION = Field(
        True, description='Allow to signup without an invitation.'
    )
    VALIDATE_INVITATION_EMAIL = Field(
        False, description='Ensure that invitation and user emails match.'
    )
    TOKEN_LIFETIME = Field(
        timedelta(hours=1),
        description='Defines for how long user token should remain valid if there was no action from user.',
    )
    INVITATION_LIFETIME = Field(
        timedelta(weeks=1), description='Defines for how long invitation remains valid.'
    )
    OWNERS_CAN_MANAGE_OWNERS = Field(
        False,
        description='Enables organization owners to manage other organization owners.',
    )
    OWNER_CAN_MANAGE_CUSTOMER = Field(
        False, description='Enables organization owners to create an organization.'
    )
    BACKEND_FIELDS_EDITABLE = Field(
        True,
        description='Allows to control /admin writable fields. '
        'If this flag is disabled it is impossible to edit any field that corresponds to '
        'backend value via /admin. Such restriction allows to save information from corruption.',
    )
    INITIAL_CUSTOMER_AGREEMENT_NUMBER = Field(
        4000,
        description='Allows to tweak initial value of agreement number. '
        'It is assumed that organization owner should accept terms of services when organization is registered via Waldur HomePort.',
    )
    CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION = Field(
        False,
        description='Enables generation of the first project on organization creation.',
    )
    ONLY_STAFF_MANAGES_SERVICES = Field(
        False, description='Allows to restrict provider management only to staff users.'
    )
    NATIVE_NAME_ENABLED = Field(
        False,
        description='Allows to render native name field in customer and user forms.',
    )
    SITE_NAME = Field(
        'Waldur MasterMind',
        description='It is used in email notifications in order to refer to the current deployment in user-friendly way.',
    )
    SITE_ADDRESS = Field(
        'Default address', description='It is used in marketplace order header.'
    )
    SITE_EMAIL = Field(
        'Default email', description='It is used in marketplace order header.'
    )
    SITE_PHONE = Field(
        'Default phone', description='It is used in marketplace order header.'
    )
    SITE_LOGO: Optional[str] = Field(
        description='It is used in marketplace order header.'
    )
    CURRENCY_NAME = Field(
        'EUR',
        description='It is used in marketplace order details and invoices for currency formatting.',
    )
    NOTIFICATIONS_PROFILE_CHANGES = Field(
        {'ENABLED': True, 'FIELDS': ('email', 'phone_number', 'job_title')},
        description='Allows enabling notifications about profile changes of organization owners.',
    )
    COUNTRIES: List[str] = Field(
        ['EE', 'LV', 'LT'],
        description='It is used in organization creation dialog in order to limit country choices to predefined set.',
    )
    ENABLE_ACCOUNTING_START_DATE = Field(
        False,
        description='Allows to enable accounting for organizations using value of accounting_start_date field.',
    )
    USE_ATOMIC_TRANSACTION = Field(
        True, description='Wrap action views in atomic transaction.'
    )
    NOTIFICATION_SUBJECT = Field(
        'Notifications from Waldur',
        description='It is used as a subject of email emitted by event logging hook.',
    )
    LOGGING_REPORT_DIRECTORY = Field(
        '/var/log/waldur', description='Directory where log files are located.'
    )
    LOGGING_REPORT_INTERVAL = Field(
        timedelta(days=7),
        description='Files older that specified interval are filtered out.',
    )
    HTTP_CHUNK_SIZE = Field(
        50,
        description='Chunk size for resource fetching from backend API. '
        'It is needed in order to avoid too long HTTP request error.',
    )
    ONLY_STAFF_CAN_INVITE_USERS = Field(
        False, description='Allow to limit invitation management to staff only.'
    )
    INVITATION_MAX_AGE: Optional[timedelta] = Field(
        None,
        description='Max age of invitation token. It is used in approve and reject actions.',
    )
    INVITATION_CREATE_MISSING_USER = Field(
        False,
        description='Allow to create FreeIPA user using details specified in invitation if user does not exist yet.',
    )
    INVITATION_DISABLE_MULTIPLE_ROLES = Field(
        False,
        description='Do not allow user to grant multiple roles in the same project or organization using invitation.',
    )
    PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS = Field(
        [],
        description='List of authentication methods which are not allowed to update user details.',
    )
    ATTACHMENT_LINK_MAX_AGE = Field(
        timedelta(hours=1), description='Max age of secure token for media download.'
    )
    EMAIL_CHANGE_MAX_AGE = Field(
        timedelta(days=1), description='Max age of change email request.'
    )
    HOMEPORT_URL = Field(
        'https://example.com/',
        description='It is used for rendering callback URL in HomePort.',
    )

    class Meta:
        public_settings = [
            'AUTHENTICATION_METHODS',
            'INVITATIONS_ENABLED',
            'ALLOW_SIGNUP_WITHOUT_INVITATION',
            'VALIDATE_INVITATION_EMAIL',
            'OWNER_CAN_MANAGE_CUSTOMER',
            'OWNERS_CAN_MANAGE_OWNERS',
            'NATIVE_NAME_ENABLED',
            'ONLY_STAFF_MANAGES_SERVICES',
            'PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS',
        ]


class WaldurAuthSocial(BaseModel):
    FACEBOOK_SECRET = Field('', description='Application secret key.')
    FACEBOOK_CLIENT_ID = Field(
        '', description='ID of application used for OAuth authentication.'
    )
    SMARTIDEE_SECRET = Field('', description='Application secret key.')
    SMARTIDEE_CLIENT_ID = Field(
        '', description='ID of application used for OAuth authentication.'
    )
    TARA_SECRET = Field('', description='Application secret key.')
    TARA_CLIENT_ID = Field(
        '', description='ID of application used for OAuth authentication.'
    )
    TARA_SANDBOX = Field(
        True,
        description='You should set it to False in order to switch to production mode.',
    )
    TARA_LABEL = Field(
        'Riigi Autentimisteenus',
        description='You may set it to eIDAS, SmartID or MobileID make it more clear to the user '
        'which exact identity provider is configured or preferred for service provider.',
    )
    KEYCLOAK_LABEL = Field(
        'Keycloak', description='Label is used by HomePort for rendering login button.'
    )
    KEYCLOAK_CLIENT_ID = Field(
        '', description='ID of application used for OAuth authentication.'
    )
    KEYCLOAK_SECRET = Field('', description='Application secret key.')
    KEYCLOAK_AUTH_URL = Field(
        '',
        description='The authorization endpoint performs authentication of the end-user. '
        'This is done by redirecting the user agent to this endpoint.',
    )
    KEYCLOAK_TOKEN_URL = Field(
        '', description='The token endpoint is used to obtain tokens.'
    )
    KEYCLOAK_USERINFO_URL = Field(
        '',
        description='The userinfo endpoint returns standard claims about the authenticated user, and is protected by a bearer token.',
    )
    EDUTEAMS_LABEL = Field(
        'Eduteams', description='Label is used by HomePort for rendering login button.'
    )
    EDUTEAMS_CLIENT_ID = Field(
        '', description='ID of application used for OAuth authentication.'
    )
    EDUTEAMS_SECRET = Field('', description='Application secret key.')
    EDUTEAMS_AUTH_URL = Field(
        'https://proxy.acc.eduteams.org/saml2sp/OIDC/authorization',
        description='The authorization endpoint performs authentication of the end-user. '
        'This is done by redirecting the user agent to this endpoint.',
    )
    EDUTEAMS_TOKEN_URL = Field(
        'https://proxy.acc.eduteams.org/OIDC/token',
        description='The token endpoint is used to obtain tokens.',
    )
    EDUTEAMS_USERINFO_URL = Field(
        'https://proxy.acc.eduteams.org/OIDC/userinfo',
        description='The userinfo endpoint returns standard claims about the authenticated user, and is protected by a bearer token.',
    )
    REMOTE_EDUTEAMS_ACCESS_TOKEN = Field(
        '', description='Token is used to authenticate against user info endpoint.'
    )
    REMOTE_EDUTEAMS_USERINFO_URL = Field(
        '', description='It allows to get user data based on userid aka CUID.'
    )

    class Meta:
        public_settings = [
            'FACEBOOK_CLIENT_ID',
            'SMARTIDEE_CLIENT_ID',
            'TARA_CLIENT_ID',
            'TARA_SANDBOX',
            'TARA_LABEL',
            'KEYCLOAK_CLIENT_ID',
            'KEYCLOAK_LABEL',
            'KEYCLOAK_AUTH_URL',
            'EDUTEAMS_CLIENT_ID',
            'EDUTEAMS_LABEL',
            'EDUTEAMS_AUTH_URL',
        ]


class WaldurConfiguration(BaseModel):
    WALDUR_CORE = WaldurCore()
    WALDUR_AUTH_SOCIAL = WaldurAuthSocial()
    USE_PROTECTED_URL = Field(
        False, description='Protect media URLs using signed token.'
    )
    VERIFY_WEBHOOK_REQUESTS = Field(
        True, description='Send verified request on webhook processing.'
    )
    CONVERT_MEDIA_URLS_TO_MASTERMIND_NETLOC = False
    IPSTACK_ACCESS_KEY = ''
    IMPORT_EXPORT_USE_TRANSACTIONS = True
