import datetime
from datetime import timedelta
from typing import List, Optional, Tuple

import saml2
from pydantic import BaseModel, Field
from saml2.entity_category.edugain import COC


class ExternalLink(BaseModel):
    label: str
    url: str


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
    GROUP_INVITATION_LIFETIME = Field(
        timedelta(weeks=1),
        description='Defines for how long group invitation remains valid.',
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
    NOTIFICATIONS_PROFILE_CHANGES = Field(
        {
            'FIELDS': ('email', 'phone_number', 'job_title'),
            'ENABLE_OPERATOR_OWNER_NOTIFICATIONS': False,
            'OPERATOR_NOTIFICATION_EMAILS': [],
        },
        description='Configure notifications about profile changes of organization owners.',
    )
    COUNTRIES: List[str] = Field(
        [
            'AL',
            'AT',
            'BA',
            'BE',
            'BG',
            'CH',
            'CY',
            'DE',
            'DK',
            'EE',
            'ES',
            'EU',
            'FI',
            'FR',
            'GB',
            'GE',
            'GR',
            'HR',
            'HU',
            'IE',
            'IS',
            'IT',
            'LT',
            'LU',
            'LV',
            'MC',
            'MK',
            'MT',
            'NL',
            'NO',
            'PL',
            'PT',
            'RO',
            'RS',
            'SE',
            'SI',
            'UA',
        ],
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
    MASTERMIND_URL = Field(
        '',
        description='It is used for rendering callback URL in MasterMind.',
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
    TRANSLATION_DOMAIN = Field(
        '',
        description='Identifier of translation domain applied to current deployment.',
    )
    GOOGLE_ANALYTICS_ID = Field(
        '',
        description='Identifier associated with your account and '
        'used by Google Analytics to collect the data.',
    )
    SUPPORT_PORTAL_URL = Field(
        '', description='Support portal URL is rendered as a shortcut on dashboard'
    )
    EXTERNAL_LINKS: List[ExternalLink] = Field(
        [],
        description='Render external links in dropdown in header. '
        'Each item should be object with label and url fields. '
        'For example: {"label": "Helpdesk", "url": "`https://example.com/`"}',
    )
    USER_MANDATORY_FIELDS: List[str] = Field(
        ['full_name', 'email'],
        description="List of user profile attributes that would be required for filling in HomePort. "
        "Note that backend will not be affected. If a mandatory field is missing in profile, "
        "a profile edit view will be forced upon user on any HomePort logged in action. "
        "Possible values are: description, email, full_name, job_title, organization, phone_number",
    )
    USER_REGISTRATION_HIDDEN_FIELDS: List[str] = Field(
        [
            'registration_method',
            'job_title',
            'phone_number',
            'organization',
        ],
        description="List of user profile attributes that would be concealed on registration form in HomePort. "
        "Possible values are: job_title, registration_method, phone_number",
    )

    INVITATION_CIVIL_NUMBER_LABEL = Field(
        '',
        description='Custom label for civil number field in invitation creation dialog.',
    )

    INVITATION_CIVIL_NUMBER_HELP_TEXT = Field(
        'Must start with a country prefix ie EE34501234215',
        description='Help text for civil number field in invitation creation dialog.',
    )

    INVITATION_TAX_NUMBER_LABEL = Field(
        '',
        description='Custom label for tax number field in invitation creation dialog.',
    )

    HOMEPORT_SENTRY_DSN: Optional[str] = Field(
        description='Sentry Data Source Name for Waldur HomePort project.'
    )

    HOMEPORT_SENTRY_ENVIRONMENT: str = Field(
        description='Sentry environment name for Waldur Homeport.',
        default='waldur-production',
    )

    HOMEPORT_SENTRY_TRACES_SAMPLE_RATE: float = Field(
        description='Percentage of transactions sent to Sentry for tracing.',
        default=0.2,
    )

    LOCAL_IDP_NAME = Field('Local DB', description='The name of local auth.')

    LOCAL_IDP_LABEL = Field('Local DB', description='The label of local auth.')

    LOCAL_IDP_MANAGEMENT_URL = Field(
        '', description='The URL for management of local user details.'
    )

    LOCAL_IDP_PROTECTED_FIELDS: List[str] = Field(
        [],
        description='The list of protected fields for local IdP.',
    )

    class Meta:
        public_settings = [
            'MASTERMIND_URL',
            'AUTHENTICATION_METHODS',
            'INVITATIONS_ENABLED',
            'VALIDATE_INVITATION_EMAIL',
            'OWNER_CAN_MANAGE_CUSTOMER',
            'OWNERS_CAN_MANAGE_OWNERS',
            'NATIVE_NAME_ENABLED',
            'ONLY_STAFF_MANAGES_SERVICES',
            'PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS',
            'TRANSLATION_DOMAIN',
            'GOOGLE_ANALYTICS_ID',
            'SUPPORT_PORTAL_URL',
            'EXTERNAL_LINKS',
            'USER_MANDATORY_FIELDS',
            'USER_REGISTRATION_HIDDEN_FIELDS',
            'INVITATION_CIVIL_NUMBER_LABEL',
            'INVITATION_CIVIL_NUMBER_HELP_TEXT',
            'INVITATION_TAX_NUMBER_LABEL',
            'HOMEPORT_SENTRY_DSN',
            'HOMEPORT_SENTRY_ENVIRONMENT',
            'HOMEPORT_SENTRY_TRACES_SAMPLE_RATE',
            'HOMEPORT_URL',
        ]


class WaldurAuthSocial(BaseModel):
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
    TARA_MANAGEMENT_URL = Field(
        '',
        description='The endpoint for user details management.',
    )
    TARA_USER_PROTECTED_FIELDS: List[str] = Field(
        [
            'full_name',
        ],
        description='The list of protected fields for TARA IdP.',
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
    KEYCLOAK_VERIFY_SSL = Field(
        True, description='Validate TLS certificate of Keycloak REST API'
    )
    KEYCLOAK_MANAGEMENT_URL = Field(
        'http://localhost:8080/auth/realms/waldur/account/#/personal-info',
        description='The endpoint for user details management.',
    )
    KEYCLOAK_USER_PROTECTED_FIELDS: List[str] = Field(
        ['full_name', 'email'],
        description='The list of protected fields for Keycloak IdP.',
    )
    EDUTEAMS_LABEL = Field(
        'eduTEAMS', description='Label is used by HomePort for rendering login button.'
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
    EDUTEAMS_MANAGEMENT_URL = Field(
        '', description='The endpoint for user details management.'
    )
    EDUTEAMS_USER_PROTECTED_FIELDS: List[str] = Field(
        [
            'full_name',
            'email',
        ],
        description='The list of protected fields for EDUTEAMS IdP.',
    )
    REMOTE_EDUTEAMS_TOKEN_URL = Field(
        'https://proxy.acc.researcher-access.org/OIDC/token',
        description='The token endpoint is used to obtain tokens.',
    )
    REMOTE_EDUTEAMS_REFRESH_TOKEN = Field(
        '', description='Token is used to authenticate against user info endpoint.'
    )
    REMOTE_EDUTEAMS_USERINFO_URL = Field(
        'https://proxy.acc.researcher-access.org/api/userinfo',
        description='It allows to get user data based on userid aka CUID.',
    )
    REMOTE_EDUTEAMS_CLIENT_ID = Field(
        '', description='ID of application used for OAuth authentication.'
    )
    REMOTE_EDUTEAMS_SECRET = Field('', description='Application secret key.')
    REMOTE_EDUTEAMS_ENABLED: bool = Field(
        False, description='Enable remote eduTEAMS extension.'
    )
    ENABLE_EDUTEAMS_SYNC = Field(
        False, description='Enable eduTEAMS synchronization with remote Waldur.'
    )

    class Meta:
        public_settings = [
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
            'REMOTE_EDUTEAMS_ENABLED',
            'ENABLE_EDUTEAMS_SYNC',
        ]


class WaldurHPC(BaseModel):
    ENABLED = Field(
        False,
        description='Enable HPC-specific hooks in Waldur deployment',
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
    EXTERNAL_LIMITS = Field(
        {},
        description='Overrided default values for SLURM offering to be created for users belonging to external organization.',
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


class WaldurKeycloak(BaseModel):
    ENABLED = Field(
        False,
        description='Enable integration of group provisioning in configured Keycloak',
    )
    BASE_URL = Field(
        'http://localhost:8080/auth', description='Base URL of Keycloak server'
    )
    REALM = Field('waldur', description='Realm used by Waldur')
    CLIENT_ID = Field('waldur', description='Identification of Waldur client app')
    CLIENT_SECRET = Field(
        'UUID', description='Credentials are generated in Keycloak admin console'
    )
    USERNAME = Field(
        'admin', description='Username of Keycloak user with administrative privileges'
    )
    PASSWORD = Field(
        'secret', description='Password of Keycloak user with administrative privileges'
    )

    class Meta:
        public_settings = ['ENABLED']


class WaldurSlurm(BaseModel):
    ENABLED = Field(
        False,
        description='Enable support for SLURM plugin in a deployment',
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
            'CPU': 16000,  # Measured unit is CPU-minutes
            'GPU': 400,  # Measured unit is GPU-minutes
            'RAM': 100000 * 2**10,  # Measured unit is MB-h
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
    ANONYMOUS_USER_CAN_VIEW_PLANS = Field(
        True,
        description='Allow anonymous users to see plans',
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
    ENABLE_STALE_RESOURCE_NOTIFICATIONS = Field(
        False,
        description='Enable reminders to owners about resources of shared offerings that have not generated any cost for the last 3 months.',
    )
    ENABLE_RESOURCE_END_DATE = Field(
        True,
        description='Allow to view and update resource end date.',
    )
    TELEMETRY_ENABLED = Field(
        True,
        description='Enable telemetry.',
    )
    TELEMETRY_URL = Field(
        'https://telemetry.waldur.com/',
        description='URL for sending telemetry data.',
    )

    TELEMETRY_VERSION = Field(
        1,
        description='Telemetry service version.',
    )

    class Meta:
        public_settings = [
            'OWNER_CAN_APPROVE_ORDER',
            'MANAGER_CAN_APPROVE_ORDER',
            'ADMIN_CAN_APPROVE_ORDER',
            'OWNER_CAN_REGISTER_SERVICE_PROVIDER',
            'ANONYMOUS_USER_CAN_VIEW_OFFERINGS',
            'ENABLE_RESOURCE_END_DATE',
        ]


class WaldurMarketplaceScript(BaseModel):
    SCRIPT_RUN_MODE = Field(
        'docker',
        description='Type of jobs deployment. Valid values: "docker" for simple docker deployment, "k8s" for Kubernetes-based one',
    )
    DOCKER_CLIENT = Field(
        {
            'base_url': 'unix://var/run/docker.sock',
        },
        description='Options for docker client. See also: <https://docker-py.readthedocs.io/en/stable/client.html#docker.client.DockerClient>',
    )
    DOCKER_RUN_OPTIONS = Field(
        {
            'mem_limit': '64m',
        },
        description='Options for docker runtime. See also: <https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.ContainerCollection.run>',
    )
    DOCKER_SCRIPT_DIR: 'str' = Field(
        None,
        description='Path to folder on executor machine where to create temporary submission scripts. If None uses OS-dependent location. OS X users, see <https://github.com/docker/for-mac/issues/1532>',
    )
    DOCKER_IMAGES = Field(
        {
            'python': 'python:3.8-alpine',
            'shell': 'alpine:3',
        },
        description='Key is command to execute script, value is image name.',
    )
    K8S_NAMESPACE = Field(
        'default', description='Kubernetes namespace where jobs will be executed'
    )
    K8S_CONFIG_PATH = Field(
        '~/.kube/config', description='Path to Kubernetes configuration file'
    )
    K8S_JOB_TIMEOUT = Field(
        30 * 60, description='Timeout for execution of one Kubernetes job in seconds'
    )


class WaldurMarketplaceRemoteSlurm(BaseModel):
    USE_WALDUR_USERNAMES = Field(
        True,
        description='Fetch usernames from Waldur rather then FreeIPA profiles.',
    )


class WaldurAuthSAML2(BaseModel):
    NAME = Field(
        'saml2',
        description='Name used for assigning the registration method to the user',
    )
    XMLSEC_BINARY = Field(
        '/usr/bin/xmlsec1', description='Full path to the xmlsec1 binary program'
    )
    ATTRIBUTE_MAP_DIR = Field(
        '/etc/waldur/saml2/attributemaps',
        description='Directory with attribute mapping',
    )
    DEBUG = Field(False, description='Set to True to output debugging information')
    IDP_METADATA_LOCAL = Field([], description='IdPs metadata XML files stored locally')
    IDP_METADATA_REMOTE = Field(
        [], description='IdPs metadata XML files stored remotely'
    )
    LOG_FILE = Field(
        '', description='Empty to disable logging SAML2-related stuff to file'
    )
    LOG_LEVEL = Field('INFO', description='Log level for SAML2')
    LOGOUT_REQUESTS_SIGNED = Field(
        'true', description='Indicates if the entity will sign the logout requests'
    )
    AUTHN_REQUESTS_SIGNED = Field(
        'true',
        description='Indicates if the authentication requests sent should be signed by default',
    )
    SIGNATURE_ALGORITHM: str = Field(
        None,
        description='Identifies the Signature algorithm URL according to the XML Signature specification (SHA1 is used by default)',
    )
    DIGEST_ALGORITHM: str = Field(
        None,
        description='Identifies the Message Digest algorithm URL according to the XML Signature specification (SHA1 is used by default)',
    )
    NAMEID_FORMAT: str = Field(
        None,
        description='Identified NameID format to use. None means default, empty string ("") disables addition of entity',
    )
    CERT_FILE = Field('', description='PEM formatted certificate chain file')
    KEY_FILE = Field('', description='PEM formatted certificate key file')
    REQUIRED_ATTRIBUTES = Field(
        [], description='SAML attributes that are required to identify a user'
    )
    OPTIONAL_ATTRIBUTES = Field(
        [], description='SAML attributes that may be useful to have but not required'
    )
    SAML_ATTRIBUTE_MAPPING = Field(
        {}, description='Mapping between SAML attributes and User fields'
    )
    ORGANIZATION = Field(
        {},
        description='Organization responsible for the service (you can set multilanguage information here)',
    )
    CATEGORIES = Field([COC], description='Links to the entity categories')
    PRIVACY_STATEMENT_URL = Field(
        'http://example.com/privacy-policy/',
        description='URL with privacy statement (required by CoC)',
    )
    DISPLAY_NAME = Field(
        'Service provider display name',
        description='Service provider display name (required by CoC)',
    )
    DESCRIPTION = Field(
        'Service provider description',
        description='Service provider description (required by CoC)',
    )
    REGISTRATION_POLICY = Field(
        'http://example.com/registration-policy/',
        description='Registration policy required by mdpi',
    )
    REGISTRATION_AUTHORITY = Field(
        'http://example.com/registration-authority/',
        description='Registration authority required by mdpi',
    )
    REGISTRATION_INSTANT = Field(
        datetime.datetime(2017, 1, 1).isoformat(),
        description='Registration instant time required by mdpi',
    )
    ENABLE_SINGLE_LOGOUT = Field(False, description='')
    ALLOW_TO_SELECT_IDENTITY_PROVIDER = Field(True, description='')
    IDENTITY_PROVIDER_URL: str = Field(None, description='')
    IDENTITY_PROVIDER_LABEL: str = Field(None, description='')
    DEFAULT_BINDING = Field(saml2.BINDING_HTTP_POST, description='')
    DISCOVERY_SERVICE_URL: str = Field(None, description='')
    DISCOVERY_SERVICE_LABEL: str = Field(None, description='')
    MANAGEMENT_URL = Field(
        '',
        description='The endpoint for user details management.',
    )

    class Meta:
        public_settings = [
            'ENABLE_SINGLE_LOGOUT',
            'ALLOW_TO_SELECT_IDENTITY_PROVIDER',
            'IDENTITY_PROVIDER_URL',
            'IDENTITY_PROVIDER_LABEL',
            'DISCOVERY_SERVICE_URL',
            'DISCOVERY_SERVICE_LABEL',
        ]


class WaldurOpenstack(BaseModel):
    DEFAULT_SECURITY_GROUPS = Field(
        (
            {
                'name': 'allow-all',
                'description': 'Security group for any access',
                'rules': (
                    {
                        'protocol': 'icmp',
                        'cidr': '0.0.0.0/0',
                        'icmp_type': -1,
                        'icmp_code': -1,
                    },
                    {
                        'protocol': 'tcp',
                        'cidr': '0.0.0.0/0',
                        'from_port': 1,
                        'to_port': 65535,
                    },
                ),
            },
            {
                'name': 'ssh',
                'description': 'Security group for secure shell access',
                'rules': (
                    {
                        'protocol': 'tcp',
                        'cidr': '0.0.0.0/0',
                        'from_port': 22,
                        'to_port': 22,
                    },
                ),
            },
            {
                'name': 'ping',
                'description': 'Security group for ping',
                'rules': (
                    {
                        'protocol': 'icmp',
                        'cidr': '0.0.0.0/0',
                        'icmp_type': -1,
                        'icmp_code': -1,
                    },
                ),
            },
            {
                'name': 'rdp',
                'description': 'Security group for remove desktop access',
                'rules': (
                    {
                        'protocol': 'tcp',
                        'cidr': '0.0.0.0/0',
                        'from_port': 3389,
                        'to_port': 3389,
                    },
                ),
            },
            {
                'name': 'web',
                'description': 'Security group for http and https access',
                'rules': (
                    {
                        'protocol': 'tcp',
                        'cidr': '0.0.0.0/0',
                        'from_port': 80,
                        'to_port': 80,
                    },
                    {
                        'protocol': 'tcp',
                        'cidr': '0.0.0.0/0',
                        'from_port': 443,
                        'to_port': 443,
                    },
                ),
            },
        ),
        description='Default security groups and rules created in each of the provisioned OpenStack tenants',
    )

    SUBNET = Field(
        {
            'ALLOCATION_POOL_START': '{first_octet}.{second_octet}.{third_octet}.10',
            'ALLOCATION_POOL_END': '{first_octet}.{second_octet}.{third_octet}.200',
        },
        description='Default allocation pool for auto-created internal network',
    )
    DEFAULT_BLACKLISTED_USERNAMES = Field(
        ['admin', 'service'],
        description='Usernames that cannot be created by Waldur in OpenStack',
    )
    # TODO: Delete these flags after migration to marketplace is completed
    # They are superseded by MANAGER_CAN_APPROVE_ORDER and ADMIN_CAN_APPROVE_ORDER
    MANAGER_CAN_MANAGE_TENANTS = Field(
        False,
        description='If true, manager can delete or change configuration of tenants.',
    )
    ADMIN_CAN_MANAGE_TENANTS = Field(
        False,
        description='If true, admin can delete or change configuration of tenants.',
    )
    TENANT_CREDENTIALS_VISIBLE = Field(
        False,
        description='If true, generated credentials of a tenant are exposed to project users',
    )

    class Meta:
        public_settings = [
            'MANAGER_CAN_MANAGE_TENANTS',
            'TENANT_CREDENTIALS_VISIBLE',
        ]


class WaldurOpenstackTenant(BaseModel):
    MAX_CONCURRENT_PROVISION = Field(
        {
            'OpenStackTenant.Instance': 4,
            'OpenStackTenant.Volume': 4,
            'OpenStackTenant.Snapshot': 4,
        },
        description='Maximum parallel executions of provisioning operations for OpenStackTenant resources',
    )
    ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS = Field(
        True,
        description='If true, customer users would be offered actions for accessing OpenStack Console',
    )
    REQUIRE_AVAILABILITY_ZONE = Field(
        False,
        description='If true, specification of availability zone during provisioning will become mandatory',
    )
    ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION = Field(
        False,
        description='If true, allow connecting of Instances directly to external networks',
    )

    class Meta:
        public_settings = [
            'ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS',
            'REQUIRE_AVAILABILITY_ZONE',
            'ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION',
        ]


class WaldurZammad(BaseModel):
    ZAMMAD_API_URL = Field(
        '',
        description='Address of Zammad server. For example <http://localhost:8080/>',
    )
    ZAMMAD_TOKEN = Field(
        '',
        description='Authorization token.',
    )
    ZAMMAD_GROUP = Field(
        '',
        description='The name of the group to which the ticket will be added. '
        'If not specified, the first group will be used.',
    )
    ZAMMAD_ARTICLE_TYPE = Field(
        'email',
        description='Type of a comment. '
        'Default is email because it allows support to reply to tickets directly in Zammad'
        '<https://docs.zammad.org/en/latest/api/ticket/articles.html#articles/>',
    )
    COMMENT_COOLDOWN_DURATION = Field(
        5,
        description='Time in minutes. '
        'Time in minutes while comment deletion is available '
        '<https://github.com/zammad/zammad/issues/2687/>, '
        '<https://github.com/zammad/zammad/issues/3086/>',
    )


class WaldurConfiguration(BaseModel):
    WALDUR_CORE = WaldurCore()
    WALDUR_AUTH_SOCIAL = WaldurAuthSocial()
    WALDUR_FREEIPA = WaldurFreeipa()
    WALDUR_KEYCLOAK = WaldurKeycloak()
    WALDUR_HPC = WaldurHPC()
    WALDUR_SLURM = WaldurSlurm()
    WALDUR_PID = WaldurPID()
    WALDUR_OPENSTACK = WaldurOpenstack()
    WALDUR_OPENSTACK_TENANT = WaldurOpenstackTenant()
    WALDUR_MARKETPLACE = WaldurMarketplace()
    WALDUR_MARKETPLACE_SCRIPT = WaldurMarketplaceScript()
    WALDUR_MARKETPLACE_REMOTE_SLURM = WaldurMarketplaceRemoteSlurm()
    WALDUR_AUTH_SAML2 = WaldurAuthSAML2()
    WALDUR_ZAMMAD = WaldurZammad()
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
    DEFAULT_REPLY_TO_EMAIL = Field(
        '',
        description='Default email address to use for email replies.',
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
        (
            ('en', 'English'),
            ('et', 'Eesti'),
        ),
        description="The list is a list of two-tuples in the format "
        "(language code, language name) – for example, ('ja', 'Japanese'). "
        "This specifies which languages are available for language selection.",
    )
    LANGUAGE_CODE = Field(
        'en', description='Represents the name of a default language.'
    )

    class Meta:
        public_settings = ['LANGUAGES', 'LANGUAGE_CODE']
