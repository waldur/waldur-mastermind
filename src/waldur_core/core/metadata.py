from datetime import timedelta
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class WaldurCore(BaseModel):
    EXTENSIONS_AUTOREGISTER = Field(
        True,
        description='Defines whether extensions should be automatically registered.',
    )
    TOKEN_KEY = Field('x-auth-token', description='Header for token authentication.')
    AUTHENTICATION_METHODS: List[str] = Field(
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
    PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS: List[str] = Field(
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
    ENABLE_GEOIP = Field(
        True, description='Enable detection of coordinates of virtual machines.'
    )
    SELLER_COUNTRY_CODE: Optional[str] = Field(
        description='Specifies seller legal or effective country of registration or residence as an '
        'ISO 3166-1 alpha-2 country code. It is used for computing VAT charge rate.'
    )
    SHOW_ALL_USERS = Field(
        False,
        description='Indicates whether user can see all other users in `api/users/` endpoint.',
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
    REMOTE_EDUTEAMS_TOKEN_URL = Field(
        'https://proxy.acc.researcher-access.org/OIDC/token',
        description='The token endpoint is used to obtain tokens.',
    )
    REMOTE_EDUTEAMS_ACCESS_TOKEN = Field(
        '', description='Token is used to authenticate against user info endpoint.'
    )
    REMOTE_EDUTEAMS_USERINFO_URL = Field(
        'https://proxy.acc.researcher-access.org/api/userinfo',
        description='It allows to get user data based on userid aka CUID.',
    )
    ENABLE_EDUTEAMS_SYNC = Field(
        False, description='Enable EduTeams synchronization with remote Waldur.'
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


class WaldurHPC(BaseModel):
    ENABLED = Field(
        False, description='Enable HPC-specific hooks in Waldur deployment',
    )
    INTERNAL_CUSTOMER_UUID = Field(
        '',
        description='UUID of a Waldur organization (aka customer) where new internal users would be added',
    )
    EXTERNAL_CUSTOMER_UUID = Field(
        '',
        description='UUID of a Waldur organization (aka customer) where new external users would be added',
    )
    INTERNAL_AFFILIATIONS: List[str] = Field(
        [],
        description='List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to internal organization.',
    )
    EXTERNAL_AFFILIATIONS: List[str] = Field(
        [],
        description='List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to external organization.',
    )
    INTERNAL_EMAIL_PATTERNS: List[str] = Field(
        [],
        description='List of user email patterns (as regex) that define if the user belongs to internal organization.',
    )
    EXTERNAL_EMAIL_PATTERNS: List[str] = Field(
        [],
        description='List of user email patterns (as regex) that define if the user belongs to external organization.',
    )
    INTERNAL_LIMITS = Field(
        {},
        description='Overrided default values for SLURM offering to be created for users belonging to internal organization.',
    )
    OFFERING_UUID = Field(
        '',
        description='UUID of a Waldur SLURM offering, which will be used for creating allocations for users',
    )
    PLAN_UUID = Field(
        '',
        description='UUID of a Waldur SLURM offering plan, which will be used for creating allocations for users',
    )


class WaldurFreeipa(BaseModel):
    ENABLED = Field(
        False,
        description='Enable integration of identity provisioning in configured FreeIPA',
    )
    HOSTNAME = Field('ipa.example.com', description='Hostname of FreeIPA server')
    USERNAME = Field(
        'admin', description='Username of FreeIPA user with administrative privileges'
    )
    PASSWORD = Field(
        'secret', description='Password of FreeIPA user with administrative privileges'
    )
    VERIFY_SSL = Field(
        True, description='Validate TLS certificate of FreeIPA web interface / REST API'
    )
    USERNAME_PREFIX = Field(
        'waldur_',
        description='Prefix to be appended to all usernames created in FreeIPA by Waldur',
    )
    GROUPNAME_PREFIX = Field(
        'waldur_',
        description='Prefix to be appended to all group names created in FreeIPA by Waldur',
    )
    BLACKLISTED_USERNAMES = Field(
        ['root'], description='List of username that users are not allowed to select'
    )
    GROUP_SYNCHRONIZATION_ENABLED = Field(
        True,
        description='Optionally disable creation of user groups in FreeIPA matching Waldur structure',
    )

    class Meta:
        public_settings = ['USERNAME_PREFIX', 'ENABLED']


class WaldurSlurm(BaseModel):
    ENABLED = Field(
        False, description='Enable support for SLURM plugin in a deployment',
    )
    CUSTOMER_PREFIX = Field(
        'waldur_customer_',
        description='Prefix for SLURM account name corresponding to Waldur organization.',
    )
    PROJECT_PREFIX = Field(
        'waldur_project_',
        description='Prefix for SLURM account name corresponding to Waldur project.',
    )
    ALLOCATION_PREFIX = Field(
        'waldur_allocation_',
        description='Prefix for SLURM account name corresponding to Waldur allocation',
    )
    PRIVATE_KEY_PATH = Field(
        '/etc/waldur/id_rsa',
        description='Path to private key file used as SSH identity file for accessing SLURM master.',
    )
    DEFAULT_LIMITS = Field(
        {
            'CPU': 16000,  # Measured unit is CPU-hours
            'GPU': 400,  # Measured unit is GPU-hours
            'RAM': 100000 * 2 ** 10,  # Measured unit is MB
        },
        description='Default limits of account that are set when SLURM account is provisioned.',
    )


class WaldurPID(BaseModel):
    DATACITE = Field(
        {
            'REPOSITORY_ID': '',
            'PASSWORD': '',
            'PREFIX': '',
            'API_URL': 'https://example.com',
            'PUBLISHER': 'Waldur',
            'COLLECTION_DOI': '',
        },
        description='Settings for integration of Waldur with Datacite PID service. Collection DOI is used to aggregate generated DOIs.',
    )


class WaldurMarketplace(BaseModel):

    THUMBNAIL_SIZE = Field(
        (120, 120),
        description='Size of the thumbnail to generate when screenshot is uploaded for an offering.',
    )
    OWNER_CAN_APPROVE_ORDER = Field(
        True,
        description='If true, orders for resource can be approved by custom organization owner.',
    )
    MANAGER_CAN_APPROVE_ORDER = Field(
        False,
        description='If true, orders for resource can be approved by manager of the customer project',
    )
    ADMIN_CAN_APPROVE_ORDER = Field(
        False,
        description='If true, orders for resource can be approved by admin of the customer project',
    )
    ANONYMOUS_USER_CAN_VIEW_OFFERINGS = Field(
        True,
        description='Allow anonymous users to see shared offerings in active, paused and archived states',
    )
    NOTIFY_STAFF_ABOUT_APPROVALS = Field(
        False,
        description='If true, users with staff role are notified when request for order approval is generated',
    )
    NOTIFY_ABOUT_RESOURCE_CHANGE = Field(
        True,
        description='If true, notify users about resource changes from Marketplace perspective. Can generate duplicate events if plugins also log',
    )
    DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE = Field(
        True, description='Disable only resource update events.'
    )
    OWNER_CAN_REGISTER_SERVICE_PROVIDER = Field(
        False,
        description='Allow organization owner to request or mark its organization as service provider',
    )
    PLAN_TEMPLATE = Field(
        'Plan: {{ plan.name }}'
        '{% for component in components %}\n'
        '{{component.name}}; '
        'amount: {{component.amount}}; '
        'price: {{component.price|floatformat }};'
        '{% endfor %}',
        description='Template for a plan field',
    )
    ENABLE_STALE_RESOURCE_NOTIFICATIONS = Field(
        False,
        description='Enable reminders to owners about resources of shared offerings that have not generated any cost for the last 3 months.',
    )

    class Meta:
        public_settings = [
            'OWNER_CAN_APPROVE_ORDER',
            'MANAGER_CAN_APPROVE_ORDER',
            'ADMIN_CAN_APPROVE_ORDER',
            'OWNER_CAN_REGISTER_SERVICE_PROVIDER',
            'ANONYMOUS_USER_CAN_VIEW_OFFERINGS',
        ]


class WaldurConfiguration(BaseModel):
    WALDUR_CORE = WaldurCore()
    WALDUR_AUTH_SOCIAL = WaldurAuthSocial()
    WALDUR_FREEIPA = WaldurFreeipa()
    WALDUR_HPC = WaldurHPC()
    WALDUR_SLURM = WaldurSlurm()
    WALDUR_PID = WaldurPID()
    WALDUR_MARKETPLACE = WaldurMarketplace()
    USE_PROTECTED_URL = Field(
        False, description='Protect media URLs using signed token.'
    )
    VERIFY_WEBHOOK_REQUESTS = Field(
        True,
        description='When webook is processed, requests verifies SSL certificates for HTTPS requests, just like a web browser.',
    )
    DEFAULT_FROM_EMAIL = Field(
        'webmaster@localhost',
        description='Default email address to use for automated correspondence from Waldur.',
    )
    IPSTACK_ACCESS_KEY: Optional[str] = Field(
        description='Unique authentication key used to gain access to the ipstack API.'
    )
    IMPORT_EXPORT_USE_TRANSACTIONS = Field(
        True,
        description='Controls if resource importing should use database transactions. '
        'Using transactions makes imports safer as a failure during import won’t import only part of the data set.',
    )
    LANGUAGES: List[Tuple[str, str]] = Field(
        (('en', 'English'), ('et', 'Eesti'),),
        description="The list is a list of two-tuples in the format "
        "(language code, language name) – for example, ('ja', 'Japanese'). "
        "This specifies which languages are available for language selection.",
    )

    class Meta:
        public_settings = ['LANGUAGES']
