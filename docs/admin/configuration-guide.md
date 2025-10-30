# Configuration options

## Static options

### WALDUR_AUTH_SAML2 plugin

Default value:

```python
WALDUR_AUTH_SAML2 = {'ALLOW_TO_SELECT_IDENTITY_PROVIDER': True,
 'ATTRIBUTE_MAP_DIR': '/etc/waldur/saml2/attributemaps',
 'AUTHN_REQUESTS_SIGNED': 'true',
 'CATEGORIES': ['http://www.geant.net/uri/dataprotection-code-of-conduct/v1'],
 'CERT_FILE': '',
 'DEBUG': False,
 'DEFAULT_BINDING': 'urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST',
 'DESCRIPTION': 'Service provider description',
 'DIGEST_ALGORITHM': None,
 'DISCOVERY_SERVICE_LABEL': None,
 'DISCOVERY_SERVICE_URL': None,
 'DISPLAY_NAME': 'Service provider display name',
 'ENABLE_SINGLE_LOGOUT': False,
 'IDENTITY_PROVIDER_LABEL': None,
 'IDENTITY_PROVIDER_URL': None,
 'IDP_METADATA_LOCAL': [],
 'IDP_METADATA_REMOTE': [],
 'KEY_FILE': '',
 'LOGOUT_REQUESTS_SIGNED': 'true',
 'LOG_FILE': '',
 'LOG_LEVEL': 'INFO',
 'MANAGEMENT_URL': '',
 'NAME': 'saml2',
 'NAMEID_FORMAT': None,
 'OPTIONAL_ATTRIBUTES': [],
 'ORGANIZATION': {},
 'PRIVACY_STATEMENT_URL': 'http://example.com/privacy-policy/',
 'REGISTRATION_AUTHORITY': 'http://example.com/registration-authority/',
 'REGISTRATION_INSTANT': '2017-01-01T00:00:00',
 'REGISTRATION_POLICY': 'http://example.com/registration-policy/',
 'REQUIRED_ATTRIBUTES': [],
 'SAML_ATTRIBUTE_MAPPING': {},
 'SIGNATURE_ALGORITHM': None,
 'XMLSEC_BINARY': '/usr/bin/xmlsec1'}
```

#### ALLOW_TO_SELECT_IDENTITY_PROVIDER

Type: bool

#### ATTRIBUTE_MAP_DIR

Type: str

Directory with attribute mapping

#### AUTHN_REQUESTS_SIGNED

Type: str

Indicates if the authentication requests sent should be signed by default

#### CATEGORIES

Type: List[str]

Links to the entity categories

#### CERT_FILE

Type: str

PEM formatted certificate chain file

#### DEBUG

Type: bool

Set to True to output debugging information

#### DEFAULT_BINDING

Type: str

#### DESCRIPTION

Type: str

Service provider description (required by CoC)

#### DIGEST_ALGORITHM

Type: Optional[str]

Identifies the Message Digest algorithm URL according to the XML Signature specification (SHA1 is used by default)

#### DISCOVERY_SERVICE_LABEL

Type: Optional[str]

#### DISCOVERY_SERVICE_URL

Type: Optional[str]

#### DISPLAY_NAME

Type: str

Service provider display name (required by CoC)

#### ENABLE_SINGLE_LOGOUT

Type: bool

#### IDENTITY_PROVIDER_LABEL

Type: Optional[str]

#### IDENTITY_PROVIDER_URL

Type: Optional[str]

#### IDP_METADATA_LOCAL

Type: List[str]

IdPs metadata XML files stored locally

#### IDP_METADATA_REMOTE

Type: List[str]

IdPs metadata XML files stored remotely

#### KEY_FILE

Type: str

PEM formatted certificate key file

#### LOGOUT_REQUESTS_SIGNED

Type: str

Indicates if the entity will sign the logout requests

#### LOG_FILE

Type: str

Empty to disable logging SAML2-related stuff to file

#### LOG_LEVEL

Type: str

Log level for SAML2

#### MANAGEMENT_URL

Type: str

The endpoint for user details management.

#### NAME

Type: str

Name used for assigning the registration method to the user

#### NAMEID_FORMAT

Type: Optional[str]

Identified NameID format to use. None means default, empty string ("") disables addition of entity

#### OPTIONAL_ATTRIBUTES

Type: List[str]

SAML attributes that may be useful to have but not required

#### ORGANIZATION

Type: Mapping[str, Any]

Organization responsible for the service (you can set multilanguage information here)

#### PRIVACY_STATEMENT_URL

Type: str

URL with privacy statement (required by CoC)

#### REGISTRATION_AUTHORITY

Type: str

Registration authority required by mdpi

#### REGISTRATION_INSTANT

Type: str

Registration instant time required by mdpi

#### REGISTRATION_POLICY

Type: str

Registration policy required by mdpi

#### REQUIRED_ATTRIBUTES

Type: List[str]

SAML attributes that are required to identify a user

#### SAML_ATTRIBUTE_MAPPING

Type: Mapping[str, str]

Mapping between SAML attributes and User fields

#### SIGNATURE_ALGORITHM

Type: Optional[str]

Identifies the Signature algorithm URL according to the XML Signature specification (SHA1 is used by default)

#### XMLSEC_BINARY

Type: str

Full path to the xmlsec1 binary program

### WALDUR_AUTH_SOCIAL plugin

Default value:

```python
WALDUR_AUTH_SOCIAL = {'ENABLE_EDUTEAMS_SYNC': False,
 'REMOTE_EDUTEAMS_CLIENT_ID': '',
 'REMOTE_EDUTEAMS_ENABLED': False,
 'REMOTE_EDUTEAMS_REFRESH_TOKEN': '',
 'REMOTE_EDUTEAMS_SECRET': '',
 'REMOTE_EDUTEAMS_SSH_API_PASSWORD': '',
 'REMOTE_EDUTEAMS_SSH_API_URL': '',
 'REMOTE_EDUTEAMS_SSH_API_USERNAME': '',
 'REMOTE_EDUTEAMS_TOKEN_URL': 'https://proxy.acc.researcher-access.org/OIDC/token',
 'REMOTE_EDUTEAMS_USERINFO_URL': 'https://proxy.acc.researcher-access.org/api/userinfo'}
```

#### ENABLE_EDUTEAMS_SYNC

Type: bool

Enable eduTEAMS synchronization with remote Waldur.

#### REMOTE_EDUTEAMS_CLIENT_ID

Type: str

ID of application used for OAuth authentication.

#### REMOTE_EDUTEAMS_ENABLED

Type: bool

Enable remote eduTEAMS extension.

#### REMOTE_EDUTEAMS_REFRESH_TOKEN

Type: str

Token is used to authenticate against user info endpoint.

#### REMOTE_EDUTEAMS_SECRET

Type: str

Application secret key.

#### REMOTE_EDUTEAMS_SSH_API_PASSWORD

Type: str

Password for SSH API URL

#### REMOTE_EDUTEAMS_SSH_API_URL

Type: str

API URL SSH keys

#### REMOTE_EDUTEAMS_SSH_API_USERNAME

Type: str

Username for SSH API URL

#### REMOTE_EDUTEAMS_TOKEN_URL

Type: str

The token endpoint is used to obtain tokens.

#### REMOTE_EDUTEAMS_USERINFO_URL

Type: str

It allows to get user data based on userid aka CUID.

### WALDUR_CORE plugin

Default value:

```python
WALDUR_CORE = {'ATTACHMENT_LINK_MAX_AGE': datetime.timedelta(seconds=3600),
 'AUTHENTICATION_METHODS': ['LOCAL_SIGNIN'],
 'BACKEND_FIELDS_EDITABLE': True,
 'COURSE_ACCOUNT_TOKEN_CLIENT_ID': '',
 'COURSE_ACCOUNT_TOKEN_SECRET': '',
 'COURSE_ACCOUNT_TOKEN_URL': '',
 'COURSE_ACCOUNT_URL': '',
 'COURSE_ACCOUNT_USE_API': False,
 'CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION': False,
 'EMAIL_CHANGE_MAX_AGE': datetime.timedelta(days=1),
 'ENABLE_ACCOUNTING_START_DATE': False,
 'ENABLE_PROJECT_KIND_COURSE': False,
 'EXTENSIONS_AUTOREGISTER': True,
 'EXTERNAL_LINKS': [],
 'GROUP_INVITATION_LIFETIME': datetime.timedelta(days=7),
 'HOMEPORT_SENTRY_DSN': None,
 'HOMEPORT_SENTRY_ENVIRONMENT': 'waldur-production',
 'HOMEPORT_SENTRY_TRACES_SAMPLE_RATE': 0.01,
 'HTTP_CHUNK_SIZE': 50,
 'INVITATIONS_ENABLED': True,
 'INVITATION_CIVIL_NUMBER_LABEL': '',
 'INVITATION_CREATE_MISSING_USER': False,
 'INVITATION_LIFETIME': datetime.timedelta(days=7),
 'INVITATION_MAX_AGE': None,
 'INVITATION_USE_WEBHOOKS': False,
 'INVITATION_WEBHOOK_TOKEN_CLIENT_ID': '',
 'INVITATION_WEBHOOK_TOKEN_SECRET': '',
 'INVITATION_WEBHOOK_TOKEN_URL': '',
 'INVITATION_WEBHOOK_URL': '',
 'LOCAL_IDP_LABEL': 'Local DB',
 'LOCAL_IDP_MANAGEMENT_URL': '',
 'LOCAL_IDP_NAME': 'Local DB',
 'LOCAL_IDP_PROTECTED_FIELDS': [],
 'LOGGING_REPORT_DIRECTORY': '/var/log/waldur',
 'LOGGING_REPORT_INTERVAL': datetime.timedelta(days=7),
 'MASTERMIND_URL': '',
 'MATOMO_SITE_ID': None,
 'MATOMO_URL_BASE': None,
 'NATIVE_NAME_ENABLED': False,
 'NOTIFICATIONS_PROFILE_CHANGES': {'ENABLE_OPERATOR_OWNER_NOTIFICATIONS': False,
                                   'FIELDS': ('email',
                                              'phone_number',
                                              'job_title'),
                                   'OPERATOR_NOTIFICATION_EMAILS': []},
 'NOTIFICATION_SUBJECT': 'Notifications from Waldur',
 'OECD_FOS_2007_CODE_MANDATORY': False,
 'ONLY_STAFF_CAN_INVITE_USERS': False,
 'PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS': [],
 'REQUEST_HEADER_IMPERSONATED_USER_UUID': 'HTTP_X_IMPERSONATED_USER_UUID',
 'RESPONSE_HEADER_IMPERSONATOR_UUID': 'X-impersonator-uuid',
 'SELLER_COUNTRY_CODE': None,
 'SERVICE_ACCOUNT_TOKEN_CLIENT_ID': '',
 'SERVICE_ACCOUNT_TOKEN_SECRET': '',
 'SERVICE_ACCOUNT_TOKEN_URL': '',
 'SERVICE_ACCOUNT_URL': '',
 'SERVICE_ACCOUNT_USE_API': False,
 'SUBNET_BLACKLIST': ['10.0.0.0/8',
                      '172.16.0.0/12',
                      '192.168.0.0/16',
                      '169.254.0.0/16',
                      '127.0.0.0/8',
                      '::1/128',
                      'fc00::/7',
                      'fe80::/10'],
 'SUPPORT_PORTAL_URL': '',
 'TOKEN_LIFETIME': datetime.timedelta(seconds=3600),
 'TRANSLATION_DOMAIN': '',
 'USER_MANDATORY_FIELDS': ['first_name', 'last_name', 'email'],
 'USER_REGISTRATION_HIDDEN_FIELDS': ['registration_method',
                                     'job_title',
                                     'phone_number',
                                     'organization'],
 'USE_ATOMIC_TRANSACTION': True,
 'VALIDATE_INVITATION_EMAIL': False}
```

#### ATTACHMENT_LINK_MAX_AGE

Type: timedelta

Max age of secure token for media download.

#### AUTHENTICATION_METHODS

Type: List[str]

List of enabled authentication methods.

#### BACKEND_FIELDS_EDITABLE

Type: bool

Allows to control /admin writable fields. If this flag is disabled it is impossible to edit any field that corresponds to backend value via /admin. Such restriction allows to save information from corruption.

#### COURSE_ACCOUNT_TOKEN_CLIENT_ID

Type: str

Client ID to get access token for course account.

#### COURSE_ACCOUNT_TOKEN_SECRET

Type: str

Client secret to get access for course account.

#### COURSE_ACCOUNT_TOKEN_URL

Type: str

Webhook URL for getting token for further course account management.

#### COURSE_ACCOUNT_URL

Type: str

Webhook URL for course account management.

#### COURSE_ACCOUNT_USE_API

Type: bool

Send course account creation and deletion requests to API.

#### CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION

Type: bool

Enables generation of the first project on organization creation.

#### EMAIL_CHANGE_MAX_AGE

Type: timedelta

Max age of change email request.

#### ENABLE_ACCOUNTING_START_DATE

Type: bool

Allows to enable accounting for organizations using value of accounting_start_date field.

#### ENABLE_PROJECT_KIND_COURSE

Type: bool

Enable course kind for projects.

#### EXTENSIONS_AUTOREGISTER

Type: bool

Defines whether extensions should be automatically registered.

#### EXTERNAL_LINKS

Type: List[ExternalLink]

Render external links in dropdown in header. Each item should be object with label and url fields. For example: {"label": "Helpdesk", "url": "`https://example.com/`"}

#### GROUP_INVITATION_LIFETIME

Type: timedelta

Defines for how long group invitation remains valid.

#### HOMEPORT_SENTRY_DSN

Type: Optional[str]

Sentry Data Source Name for Waldur HomePort project.

#### HOMEPORT_SENTRY_ENVIRONMENT

Type: str

Sentry environment name for Waldur Homeport.

#### HOMEPORT_SENTRY_TRACES_SAMPLE_RATE

Type: float

Percentage of transactions sent to Sentry for tracing.

#### HTTP_CHUNK_SIZE

Type: int

Chunk size for resource fetching from backend API. It is needed in order to avoid too long HTTP request error.

#### INVITATIONS_ENABLED

Type: bool

Allows to disable invitations feature.

#### INVITATION_CIVIL_NUMBER_LABEL

Type: str

Custom label for civil number field in invitation creation dialog.

#### INVITATION_CREATE_MISSING_USER

Type: bool

Allow to create FreeIPA user using details specified in invitation if user does not exist yet.

#### INVITATION_LIFETIME

Type: timedelta

Defines for how long invitation remains valid.

#### INVITATION_MAX_AGE

Type: Optional[timedelta]

Max age of invitation token. It is used in approve and reject actions.

#### INVITATION_USE_WEBHOOKS

Type: bool

Allow sending of webhooks instead of sending of emails.

#### INVITATION_WEBHOOK_TOKEN_CLIENT_ID

Type: str

Client ID to get access token from Keycloak.

#### INVITATION_WEBHOOK_TOKEN_SECRET

Type: str

Client secret to get access token from Keycloak.

#### INVITATION_WEBHOOK_TOKEN_URL

Type: str

Keycloak URL to get access token.

#### INVITATION_WEBHOOK_URL

Type: str

Webhook URL for sending invitations.

#### LOCAL_IDP_LABEL

Type: str

The label of local auth.

#### LOCAL_IDP_MANAGEMENT_URL

Type: str

The URL for management of local user details.

#### LOCAL_IDP_NAME

Type: str

The name of local auth.

#### LOCAL_IDP_PROTECTED_FIELDS

Type: List[str]

The list of protected fields for local IdP.

#### LOGGING_REPORT_DIRECTORY

Type: str

Directory where log files are located.

#### LOGGING_REPORT_INTERVAL

Type: timedelta

Files older that specified interval are filtered out.

#### MASTERMIND_URL

Type: str

It is used for rendering callback URL in MasterMind.

#### MATOMO_SITE_ID

Type: Optional[int]

Site ID is used by Matomo analytics application.

#### MATOMO_URL_BASE

Type: Optional[str]

URL base is used by Matomo analytics application.

#### NATIVE_NAME_ENABLED

Type: bool

Allows to render native name field in customer and user forms.

#### NOTIFICATIONS_PROFILE_CHANGES

Type: Mapping[str, Any]

Configure notifications about profile changes of organization owners.

#### NOTIFICATION_SUBJECT

Type: str

It is used as a subject of email emitted by event logging hook.

#### OECD_FOS_2007_CODE_MANDATORY

Type: bool

Field oecd_fos_2007_code must be required for project.

#### ONLY_STAFF_CAN_INVITE_USERS

Type: bool

Allow to limit invitation management to staff only.

#### PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS

Type: List[str]

List of authentication methods for which a manual update of user details is not allowed.

#### REQUEST_HEADER_IMPERSONATED_USER_UUID

Type: str

The request header, which contains the user UUID of the user to be impersonated.

#### RESPONSE_HEADER_IMPERSONATOR_UUID

Type: str

The response header, which contains the UUID of the user who requested the impersonation.

#### SELLER_COUNTRY_CODE

Type: Optional[str]

Specifies seller legal or effective country of registration or residence as an ISO 3166-1 alpha-2 country code. It is used for computing VAT charge rate.

#### SERVICE_ACCOUNT_TOKEN_CLIENT_ID

Type: str

Client ID to get access token for service account.

#### SERVICE_ACCOUNT_TOKEN_SECRET

Type: str

Client secret to get access for service account.

#### SERVICE_ACCOUNT_TOKEN_URL

Type: str

Webhook URL for getting token for further service account management.

#### SERVICE_ACCOUNT_URL

Type: str

Webhook URL for service account management.

#### SERVICE_ACCOUNT_USE_API

Type: bool

Send service account creation and deletion requests to API.

#### SUBNET_BLACKLIST

Type: List[str]

List of IP ranges that are blocked for the SDK client.

#### SUPPORT_PORTAL_URL

Type: str

Support portal URL is rendered as a shortcut on dashboard

#### TOKEN_LIFETIME

Type: timedelta

Defines for how long user token should remain valid if there was no action from user.

#### TRANSLATION_DOMAIN

Type: str

Identifier of translation domain applied to current deployment.

#### USER_MANDATORY_FIELDS

Type: List[str]

List of user profile attributes that would be required for filling in HomePort. Note that backend will not be affected. If a mandatory field is missing in profile, a profile edit view will be forced upon user on any HomePort logged in action. Possible values are: description, email, full_name, job_title, organization, phone_number

#### USER_REGISTRATION_HIDDEN_FIELDS

Type: List[str]

List of user profile attributes that would be concealed on registration form in HomePort. Possible values are: job_title, registration_method, phone_number

#### USE_ATOMIC_TRANSACTION

Type: bool

Wrap action views in atomic transaction.

#### VALIDATE_INVITATION_EMAIL

Type: bool

Ensure that invitation and user emails match.

### WALDUR_HPC plugin

Default value:

```python
WALDUR_HPC = {'ENABLED': False,
 'EXTERNAL_AFFILIATIONS': [],
 'EXTERNAL_CUSTOMER_UUID': '',
 'EXTERNAL_EMAIL_PATTERNS': [],
 'EXTERNAL_LIMITS': {},
 'INTERNAL_AFFILIATIONS': [],
 'INTERNAL_CUSTOMER_UUID': '',
 'INTERNAL_EMAIL_PATTERNS': [],
 'INTERNAL_LIMITS': {},
 'OFFERING_UUID': '',
 'PLAN_UUID': ''}
```

#### ENABLED

Type: bool

Enable HPC-specific hooks in Waldur deployment

#### EXTERNAL_AFFILIATIONS

Type: List[str]

List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to external organization.

#### EXTERNAL_CUSTOMER_UUID

Type: str

UUID of a Waldur organization (aka customer) where new external users would be added

#### EXTERNAL_EMAIL_PATTERNS

Type: List[str]

List of user email patterns (as regex) that define if the user belongs to external organization.

#### EXTERNAL_LIMITS

Type: Mapping[str, Any]

Overrided default values for SLURM offering to be created for users belonging to external organization.

#### INTERNAL_AFFILIATIONS

Type: List[str]

List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to internal organization.

#### INTERNAL_CUSTOMER_UUID

Type: str

UUID of a Waldur organization (aka customer) where new internal users would be added

#### INTERNAL_EMAIL_PATTERNS

Type: List[str]

List of user email patterns (as regex) that define if the user belongs to internal organization.

#### INTERNAL_LIMITS

Type: Mapping[str, Any]

Overrided default values for SLURM offering to be created for users belonging to internal organization.

#### OFFERING_UUID

Type: str

UUID of a Waldur SLURM offering, which will be used for creating allocations for users

#### PLAN_UUID

Type: str

UUID of a Waldur SLURM offering plan, which will be used for creating allocations for users

### WALDUR_OPENSTACK plugin

Default value:

```python
WALDUR_OPENSTACK = {'ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS': True,
 'ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION': False,
 'DEFAULT_BLACKLISTED_USERNAMES': ['admin', 'service'],
 'DEFAULT_SECURITY_GROUPS': ({'description': 'Security group for secure shell '
                                             'access',
                              'name': 'ssh',
                              'rules': ({'cidr': '0.0.0.0/0',
                                         'from_port': 22,
                                         'protocol': 'tcp',
                                         'to_port': 22},)},
                             {'description': 'Security group for ping',
                              'name': 'ping',
                              'rules': ({'cidr': '0.0.0.0/0',
                                         'icmp_code': -1,
                                         'icmp_type': -1,
                                         'protocol': 'icmp'},)},
                             {'description': 'Security group for remote '
                                             'desktop access',
                              'name': 'rdp',
                              'rules': ({'cidr': '0.0.0.0/0',
                                         'from_port': 3389,
                                         'protocol': 'tcp',
                                         'to_port': 3389},)},
                             {'description': 'Security group for http and '
                                             'https access',
                              'name': 'web',
                              'rules': ({'cidr': '0.0.0.0/0',
                                         'from_port': 80,
                                         'protocol': 'tcp',
                                         'to_port': 80},
                                        {'cidr': '0.0.0.0/0',
                                         'from_port': 443,
                                         'protocol': 'tcp',
                                         'to_port': 443})}),
 'MAX_CONCURRENT_PROVISION': {'OpenStack.Instance': 4,
                              'OpenStack.Snapshot': 4,
                              'OpenStack.Volume': 4},
 'REQUIRE_AVAILABILITY_ZONE': False,
 'SUBNET': {'ALLOCATION_POOL_END': '{first_octet}.{second_octet}.{third_octet}.200',
            'ALLOCATION_POOL_START': '{first_octet}.{second_octet}.{third_octet}.10'},
 'TENANT_CREDENTIALS_VISIBLE': False}
```

#### ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS

Type: bool

If true, customer users would be offered actions for accessing OpenStack console

#### ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION

Type: bool

If true, allow connecting of instances directly to external networks

#### DEFAULT_BLACKLISTED_USERNAMES

Type: List[str]

Usernames that cannot be created by Waldur in OpenStack

#### DEFAULT_SECURITY_GROUPS

Type: Tuple[dict[str, str | tuple[dict[str, str | int], ...]], ...]

Default security groups and rules created in each of the provisioned OpenStack tenants

#### MAX_CONCURRENT_PROVISION

Type: Mapping[str, int]

Maximum parallel executions of provisioning operations for OpenStack resources

#### REQUIRE_AVAILABILITY_ZONE

Type: bool

If true, specification of availability zone during provisioning will become mandatory

#### SUBNET

Type: Mapping[str, str]

Default allocation pool for auto-created internal network

#### TENANT_CREDENTIALS_VISIBLE

Type: bool

If true, generated credentials of a tenant are exposed to project users

### WALDUR_PID plugin

Default value:

```python
WALDUR_PID = {'DATACITE': {'API_URL': 'https://example.com',
              'COLLECTION_DOI': '',
              'PASSWORD': '',
              'PREFIX': '',
              'PUBLISHER': 'Waldur',
              'REPOSITORY_ID': ''}}
```

#### DATACITE

Type: Mapping[str, str]

Settings for integration of Waldur with Datacite PID service. Collection DOI is used to aggregate generated DOIs.

### WALDUR_SLURM plugin

Default value:

```python
WALDUR_SLURM = {'ALLOCATION_PREFIX': 'waldur_allocation_',
 'CUSTOMER_PREFIX': 'waldur_customer_',
 'DEFAULT_LIMITS': {'CPU': 16000, 'GPU': 400, 'RAM': 102400000},
 'ENABLED': False,
 'PRIVATE_KEY_PATH': '/etc/waldur/id_rsa',
 'PROJECT_PREFIX': 'waldur_project_'}
```

#### ALLOCATION_PREFIX

Type: str

Prefix for SLURM account name corresponding to Waldur allocation

#### CUSTOMER_PREFIX

Type: str

Prefix for SLURM account name corresponding to Waldur organization.

#### DEFAULT_LIMITS

Type: Mapping[str, int]

Default limits of account that are set when SLURM account is provisioned.

#### ENABLED

Type: bool

Enable support for SLURM plugin in a deployment

#### PRIVATE_KEY_PATH

Type: str

Path to private key file used as SSH identity file for accessing SLURM master.

#### PROJECT_PREFIX

Type: str

Prefix for SLURM account name corresponding to Waldur project.

### Other variables

#### DEFAULT_FROM_EMAIL

Type: str, default value: webmaster@localhost

Default email address to use for automated correspondence from Waldur.

#### DEFAULT_REPLY_TO_EMAIL

Type: str

Default email address to use for email replies.

#### EMAIL_HOOK_FROM_EMAIL

Type: str

Alternative email address to use for email hooks.

#### IMPORT_EXPORT_USE_TRANSACTIONS

Type: bool, default value: True

Controls if resource importing should use database transactions. Using transactions makes imports safer as a failure during import won't import only part of the data set.

#### IPSTACK_ACCESS_KEY

Type: Optional[str]

Unique authentication key used to gain access to the ipstack API.

#### LANGUAGES

Type: List[tuple[str, str]], default value: [('en', 'English'), ('et', 'Eesti')]

The list is a list of two-tuples in the format (language code, language name) – for example, ('ja', 'Japanese').

#### LANGUAGE_CODE

Type: str, default value: en

Represents the name of a default language.

#### VERIFY_WEBHOOK_REQUESTS

Type: bool, default value: True

When webook is processed, requests verifies SSL certificates for HTTPS requests, just like a web browser.

## Dynamic options

### Branding

#### SITE_NAME

**Type:** str

**Default value**: Waldur

Human-friendly name of the Waldur deployment.

#### SHORT_PAGE_TITLE

**Type:** str

**Default value**: Waldur

It is used as prefix for page title.

#### FULL_PAGE_TITLE

**Type:** str

**Default value**: Waldur | Cloud Service Management

It is used as default page title if it's not specified explicitly.

#### SITE_DESCRIPTION

**Type:** str

**Default value**: Your single pane of control for managing projects, teams and resources in a self-service manner.

Description of the Waldur deployment.

#### HOMEPORT_URL

**Type:** str

**Default value**: https://example.com/

It is used for rendering callback URL in HomePort

#### RANCHER_USERNAME_INPUT_LABEL

**Type:** str

**Default value**: Username

Label for the username field in Rancher external user resource access management.

### Marketplace Branding

#### SITE_ADDRESS

**Type:** str

It is used in marketplace order header.

#### SITE_EMAIL

**Type:** str

It is used in marketplace order header and UI footer.

#### SITE_PHONE

**Type:** str

It is used in marketplace order header and UI footer.

#### CURRENCY_NAME

**Type:** str

**Default value**: EUR

It is used in marketplace order details and invoices for currency formatting.

#### MARKETPLACE_LANDING_PAGE

**Type:** str

**Default value**: Marketplace

Marketplace landing page title.

#### COUNTRIES

**Type:** country_list_field

**Default value**: ['AL', 'AT', 'BA', 'BE', 'BG', 'CH', 'CY', 'CZ', 'DE', 'DK', 'EE', 'ES', 'EU', 'FI', 'FR', 'GB', 'GE', 'GR', 'HR', 'HU', 'IE', 'IS', 'IT', 'LT', 'LU', 'LV', 'MC', 'MK', 'MT', 'NL', 'NO', 'PL', 'PT', 'RO', 'RS', 'SE', 'SI', 'SK', 'UA']

It is used in organization creation dialog in order to limit country choices to predefined set.

### Marketplace

#### THUMBNAIL_SIZE

**Type:** str

**Default value**: 120x120

Size of the thumbnail to generate when screenshot is uploaded for an offering.

#### ANONYMOUS_USER_CAN_VIEW_OFFERINGS

**Type:** bool

**Default value**: True

Allow anonymous users to see shared offerings in active, paused and archived states

#### ANONYMOUS_USER_CAN_VIEW_PLANS

**Type:** bool

**Default value**: True

Allow anonymous users to see plans

#### NOTIFY_STAFF_ABOUT_APPROVALS

**Type:** bool

If true, users with staff role are notified when request for order approval is generated

#### NOTIFY_ABOUT_RESOURCE_CHANGE

**Type:** bool

**Default value**: True

If true, notify users about resource changes from Marketplace perspective. Can generate duplicate events if plugins also log

#### DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE

**Type:** bool

**Default value**: True

Disable only resource update events.

#### ENABLE_STALE_RESOURCE_NOTIFICATIONS

**Type:** bool

Enable reminders to owners about resources of shared offerings that have not generated any cost for the last 3 months.

#### ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND

**Type:** bool

Enable mock returns for the service account service

#### ENABLE_MOCK_COURSE_ACCOUNT_BACKEND

**Type:** bool

Enable mock returns for the course account service

#### ENFORCE_USER_CONSENT_FOR_OFFERINGS

**Type:** bool

If True, users must have active consent to access offerings that have active Terms of Service.

#### ENABLE_ORDER_START_DATE

**Type:** bool

Allow setting start date to control when resource creation order is processed.

### Project

#### PROJECT_END_DATE_MANDATORY

**Type:** bool

If true, project end date field becomes mandatory when creating or updating projects.

### Telemetry

#### TELEMETRY_URL

**Type:** str

**Default value**: https://telemetry.waldur.com/

URL for sending telemetry data.

#### TELEMETRY_VERSION

**Type:** int

**Default value**: 1

Telemetry service version.

### Custom Scripts

#### SCRIPT_RUN_MODE

**Type:** str

**Default value**: docker

Type of jobs deployment. Valid values: "docker" for simple docker deployment, "k8s" for Kubernetes-based one

#### DOCKER_CLIENT

**Type:** dict_field

**Default value**: {'base_url': 'unix:///var/run/docker.sock'}

Options for docker client. See also: <https://docker-py.readthedocs.io/en/stable/client.html#docker.client.DockerClient>

#### DOCKER_RUN_OPTIONS

**Type:** dict_field

**Default value**: {'mem_limit': '512m'}

Options for docker runtime. See also: <https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.ContainerCollection.run>

#### DOCKER_SCRIPT_DIR

**Type:** str

Path to folder on executor machine where to create temporary submission scripts. If None, uses OS-dependent location. OS X users, see <https://github.com/docker/for-mac/issues/1532>

#### DOCKER_REMOVE_CONTAINER

**Type:** bool

**Default value**: True

Remove Docker container after script execution

#### DOCKER_IMAGES

**Type:** dict_field

**Default value**: {'python': {'image': 'python:3.11-alpine', 'command': 'python'}, 'shell': {'image': 'alpine:3', 'command': 'sh'}, 'ansible': {'image': 'alpine/ansible:2.18.6', 'command': 'ansible-playbook'}}

Key is command to execute script, value is a dictionary of image name and command.

#### DOCKER_VOLUME_NAME

**Type:** str

**Default value**: waldur-docker-compose_waldur_script_launchzone

A name of the shared volume to store scripts

#### K8S_NAMESPACE

**Type:** str

**Default value**: default

Kubernetes namespace where jobs will be executed

#### K8S_CONFIG_PATH

**Type:** str

**Default value**: ~/.kube/config

Path to Kubernetes configuration file

#### K8S_JOB_TIMEOUT

**Type:** int

**Default value**: 1800

Timeout for execution of one Kubernetes job in seconds

### Notifications

#### COMMON_FOOTER_TEXT

**Type:** text_field

Common footer in txt format for all emails.

#### COMMON_FOOTER_HTML

**Type:** html_field

Common footer in html format for all emails.

#### MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES

**Type:** int

**Default value**: 60

How many minutes before scheduled maintenance users should be notified.

#### MAINTENANCE_ANNOUNCEMENT_NOTIFY_SYSTEM

**Type:** list_field

**Default value**: ['AdminAnnouncement']

How maintenance notifications are delivered. Choices: AdminAnnouncement or BroadcastMessage.

### Links

#### DOCS_URL

**Type:** url_field

Renders link to docs in header

#### HERO_LINK_LABEL

**Type:** str

Label for link in hero section of HomePort landing page. It can be lead to support site or blog post.

#### HERO_LINK_URL

**Type:** url_field

Link URL in hero section of HomePort landing page.

#### SUPPORT_PORTAL_URL

**Type:** url_field

Link URL to support portal. Rendered as a shortcut on dashboard

### Theme

#### SIDEBAR_STYLE

**Type:** str

**Default value**: dark

Style of sidebar. Possible values: dark, light, accent.

#### BRAND_COLOR

**Type:** color_field

**Default value**: #307300

Brand color is used for button background.

#### DISABLE_DARK_THEME

**Type:** bool

Toggler to disable dark theme.

### Images

#### SITE_LOGO

**Type:** image_field

The image used in marketplace order header.

#### SIDEBAR_LOGO

**Type:** image_field

The image rendered at the top of sidebar menu in HomePort.

#### SIDEBAR_LOGO_MOBILE

**Type:** image_field

The image rendered at the top of mobile sidebar menu in HomePort.

#### SIDEBAR_LOGO_DARK

**Type:** image_field

The image rendered at the top of sidebar menu in dark mode.

#### POWERED_BY_LOGO

**Type:** image_field

The image rendered at the bottom of login menu in HomePort.

#### HERO_IMAGE

**Type:** image_field

The image rendered at hero section of HomePort landing page.

#### MARKETPLACE_HERO_IMAGE

**Type:** image_field

The image rendered at hero section of Marketplace landing page. Please, use a wide image (min. 1920×600px) with no text or logos. Keep the center area clean, and choose a darker image for dark mode or a brighter image for light mode.

#### CALL_MANAGEMENT_HERO_IMAGE

**Type:** image_field

The image rendered at hero section of Call Management landing page. Please, use a wide image (min. 1920×600px) with no text or logos. Keep the center area clean, and choose a darker image for dark mode or a brighter image for light mode.

#### LOGIN_LOGO

**Type:** image_field

A custom .png image file for login page

#### FAVICON

**Type:** image_field

A custom favicon .png image file

#### OFFERING_LOGO_PLACEHOLDER

**Type:** image_field

Default logo for offering

#### KEYCLOAK_ICON

**Type:** image_field

A custom PNG icon for Keycloak login button

### Service desk integration settings

#### WALDUR_SUPPORT_ENABLED

**Type:** bool

**Default value**: True

Toggler for support plugin.

#### WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE

**Type:** str

**Default value**: atlassian

Type of support backend. Possible values: atlassian, zammad, smax.

#### WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE

**Type:** bool

**Default value**: True

Toggler for request type displaying

### Atlassian settings

#### ATLASSIAN_API_URL

**Type:** url_field

**Default value**: https://example.com/

Atlassian API server URL

#### ATLASSIAN_USERNAME

**Type:** str

**Default value**: USERNAME

Username for access user

#### ATLASSIAN_PASSWORD

**Type:** secret_field

**Default value**: PASSWORD

Password for access user

#### ATLASSIAN_EMAIL

**Type:** email_field

Email for access user

#### ATLASSIAN_TOKEN

**Type:** secret_field

Token for access user

#### ATLASSIAN_PERSONAL_ACCESS_TOKEN

**Type:** secret_field

Personal Access Token for user

#### ATLASSIAN_OAUTH2_CLIENT_ID

**Type:** secret_field

OAuth 2.0 Client ID

#### ATLASSIAN_OAUTH2_ACCESS_TOKEN

**Type:** secret_field

OAuth 2.0 Access Token

#### ATLASSIAN_OAUTH2_TOKEN_TYPE

**Type:** str

**Default value**: Bearer

OAuth 2.0 Token Type

#### ATLASSIAN_PROJECT_ID

**Type:** str

Service desk ID or key

#### ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE

**Type:** str

**Default value**: Service Request

Issue type used for request-based item processing.

#### ATLASSIAN_EXCLUDED_ATTACHMENT_TYPES

**Type:** str

Comma-separated list of file extenstions not allowed for attachment.

#### ATLASSIAN_ISSUE_TYPES

**Type:** str

**Default value**: Informational, Service Request, Change Request, Incident

Comma-separated list of enabled issue types. First type is the default one.

#### ATLASSIAN_SUPPORT_TYPE_MAPPING

**Type:** dict_field

**Default value**: {'Informational': 'Get IT help', 'Service Request': 'Request new software', 'Change Request': 'Change Request', 'Incident': 'Report a system problem'}

Mapping from frontend issue types to backend request types

#### ATLASSIAN_AFFECTED_RESOURCE_FIELD

**Type:** str

Affected resource field name

#### ATLASSIAN_DESCRIPTION_TEMPLATE

**Type:** str

Template for issue description

#### ATLASSIAN_SUMMARY_TEMPLATE

**Type:** str

Template for issue summary

#### ATLASSIAN_IMPACT_FIELD

**Type:** str

**Default value**: Impact

Impact field name

#### ATLASSIAN_ORGANISATION_FIELD

**Type:** str

Organisation field name

#### ATLASSIAN_RESOLUTION_SLA_FIELD

**Type:** str

Resolution SLA field name

#### ATLASSIAN_PROJECT_FIELD

**Type:** str

Project field name

#### ATLASSIAN_REPORTER_FIELD

**Type:** str

**Default value**: Original Reporter

Reporter field name

#### ATLASSIAN_CALLER_FIELD

**Type:** str

**Default value**: Caller

Caller field name

#### ATLASSIAN_SLA_FIELD

**Type:** str

**Default value**: Time to first response

SLA field name

#### ATLASSIAN_LINKED_ISSUE_TYPE

**Type:** str

**Default value**: Relates

Type of linked issue field name

#### ATLASSIAN_SATISFACTION_FIELD

**Type:** str

**Default value**: Customer satisfaction

Customer satisfaction field name

#### ATLASSIAN_REQUEST_FEEDBACK_FIELD

**Type:** str

**Default value**: Request feedback

Request feedback field name

#### ATLASSIAN_TEMPLATE_FIELD

**Type:** str

Template field name

#### ATLASSIAN_WALDUR_BACKEND_ID_FIELD

**Type:** str

**Default value**: customfield_10200

Waldur backend ID custom field ID (fallback when field lookup by name fails)

#### ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED

**Type:** bool

**Default value**: True

Should extra issue field mappings be applied

#### ATLASSIAN_SHARED_USERNAME

**Type:** bool

Is Service Desk username the same as in Waldur

#### ATLASSIAN_VERIFY_SSL

**Type:** bool

**Default value**: True

Toggler for SSL verification

#### ATLASSIAN_USE_OLD_API

**Type:** bool

Toggler for legacy API usage.

#### ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS

**Type:** bool

Toggler for mapping between waldur user and service desk agents.

### Zammad settings

#### ZAMMAD_API_URL

**Type:** url_field

Zammad API server URL. For example <https://localhost:8080/>

#### ZAMMAD_TOKEN

**Type:** secret_field

Authorization token.

#### ZAMMAD_GROUP

**Type:** str

The name of the group to which the ticket will be added. If not specified, the first group will be used.

#### ZAMMAD_ARTICLE_TYPE

**Type:** str

**Default value**: email

Type of a comment. Default is email because it allows support to reply to tickets directly in Zammad<https://docs.zammad.org/en/latest/api/ticket/articles.html#articles/>

#### ZAMMAD_COMMENT_MARKER

**Type:** str

**Default value**: Created by Waldur

Marker for comment. Used for separating comments made via Waldur from natively added comments.

#### ZAMMAD_COMMENT_PREFIX

**Type:** str

**Default value**: User: {name}

Comment prefix with user info.

#### ZAMMAD_COMMENT_COOLDOWN_DURATION

**Type:** int

**Default value**: 5

Time in minutes. Time in minutes while comment deletion is available <https://github.com/zammad/zammad/issues/2687/>, <https://github.com/zammad/zammad/issues/3086/>

### SMAX settings

#### SMAX_API_URL

**Type:** url_field

SMAX API server URL. For example <https://localhost:8080/>

#### SMAX_TENANT_ID

**Type:** str

User tenant ID.

#### SMAX_LOGIN

**Type:** str

Authorization login.

#### SMAX_PASSWORD

**Type:** secret_field

Authorization password.

#### SMAX_ORGANISATION_FIELD

**Type:** str

Organisation field name.

#### SMAX_PROJECT_FIELD

**Type:** str

Project field name.

#### SMAX_AFFECTED_RESOURCE_FIELD

**Type:** str

Resource field name.

#### SMAX_REQUESTS_OFFERING

**Type:** str

Requests offering code for all issues.

#### SMAX_SECONDS_TO_WAIT

**Type:** int

**Default value**: 1

Duration in seconds of delay between pull user attempts.

#### SMAX_TIMES_TO_PULL

**Type:** int

**Default value**: 10

The maximum number of attempts to pull user from backend.

#### SMAX_CREATION_SOURCE_NAME

**Type:** str

Creation source name.

#### SMAX_VERIFY_SSL

**Type:** bool

**Default value**: True

Toggler for SSL verification

### Proposal settings

#### PROPOSAL_REVIEW_DURATION

**Type:** int

**Default value**: 7

Review duration in days.

### Table settings

#### USER_TABLE_COLUMNS

**Type:** str

Comma-separated list of columns for users table.

### Localization

#### LANGUAGE_CHOICES

**Type:** str

**Default value**: en,et,lt,lv,ru,it,de,da,sv,es,fr,nb,ar,cs

List of enabled languages

### User settings

#### AUTO_APPROVE_USER_TOS

**Type:** bool

Mark terms of services as approved for new users.

#### ENABLE_STRICT_CHECK_ACCEPTING_INVITATION

**Type:** bool

If true, user email in Waldur database and in invitatation must strictly match.

#### INVITATION_DISABLE_MULTIPLE_ROLES

**Type:** bool

Do not allow user to grant multiple roles in the same project or organization using invitation.

#### DEFAULT_IDP

**Type:** str

Triggers authentication flow at once.

#### DEACTIVATE_USER_IF_NO_ROLES

**Type:** bool

Deactivate user if all roles are revoked (except staff/support)

#### OIDC_BLOCK_CREATION_OF_UNINVITED_USERS

**Type:** bool

If true, block creation of an account on OIDC login if user email is not provided or provided and is not in the list of one of the active invitations.

#### OIDC_ACCESS_TOKEN_ENABLED

**Type:** bool

If true, OIDC complete view returns access token instead of Waldur token

### FreeIPA settings

#### FREEIPA_ENABLED

**Type:** bool

Enable integration of identity provisioning in configured FreeIPA.

#### FREEIPA_HOSTNAME

**Type:** str

**Default value**: ipa.example.com

Hostname of FreeIPA server.

#### FREEIPA_USERNAME

**Type:** str

**Default value**: admin

Username of FreeIPA user with administrative privileges.

#### FREEIPA_PASSWORD

**Type:** secret_field

**Default value**: secret

Password of FreeIPA user with administrative privileges

#### FREEIPA_VERIFY_SSL

**Type:** bool

**Default value**: True

Validate TLS certificate of FreeIPA web interface / REST API

#### FREEIPA_USERNAME_PREFIX

**Type:** str

**Default value**: waldur_

Prefix to be appended to all usernames created in FreeIPA by Waldur

#### FREEIPA_GROUPNAME_PREFIX

**Type:** str

**Default value**: waldur_

Prefix to be appended to all group names created in FreeIPA by Waldur

#### FREEIPA_BLACKLISTED_USERNAMES

**Type:** list_field

**Default value**: ['root']

List of username that users are not allowed to select

#### FREEIPA_GROUP_SYNCHRONIZATION_ENABLED

**Type:** bool

**Default value**: True

Optionally disable creation of user groups in FreeIPA matching Waldur structure

### OIDC auth settings

#### OIDC_AUTH_URL

**Type:** str

OIDC authentication endpoint URL.

#### OIDC_INTROSPECTION_URL

**Type:** str

OIDC introspection endpoint URL for validating access tokens.

#### OIDC_CLIENT_ID

**Type:** str

Client ID for authenticating against the introspection endpoint.

#### OIDC_CLIENT_SECRET

**Type:** str

Client secret for authenticating against the introspection endpoint.

#### OIDC_USER_FIELD

**Type:** str

**Default value**: username

Field name from the introspection response to identify the user (e.g., 'username', 'email', 'client_id').

#### OIDC_CACHE_TIMEOUT

**Type:** int

**Default value**: 300

Number of seconds to cache token introspection results.

### Onboarding settings

#### ONBOARDING_VERIFICATION_EXPIRY_HOURS

**Type:** int

**Default value**: 48

Number of hours after which onboarding verifications expire.

#### ONBOARDING_ARIREGISTER_BASE_URL

**Type:** url_field

**Default value**: https://demo-ariregxmlv6.rik.ee/

Base URL for Estonian Äriregister API endpoint.

#### ONBOARDING_ARIREGISTER_USERNAME

**Type:** text_field

Username for Estonian Äriregister API authentication.

#### ONBOARDING_ARIREGISTER_PASSWORD

**Type:** secret_field

Password for Estonian Äriregister API authentication.

#### ONBOARDING_ARIREGISTER_TIMEOUT

**Type:** int

**Default value**: 30

Timeout in seconds for Estonian Äriregister API requests.


