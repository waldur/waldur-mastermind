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

**Type:** bool

#### ATTRIBUTE_MAP_DIR

**Type:** str

Directory with attribute mapping

#### AUTHN_REQUESTS_SIGNED

**Type:** str

Indicates if the authentication requests sent should be signed by default

#### CATEGORIES

**Type:** List[str]

Links to the entity categories

#### CERT_FILE

**Type:** str

PEM formatted certificate chain file

#### DEBUG

**Type:** bool

Set to True to output debugging information

#### DEFAULT_BINDING

**Type:** str

#### DESCRIPTION

**Type:** str

Service provider description (required by CoC)

#### DIGEST_ALGORITHM

**Type:** Optional[str]

Identifies the Message Digest algorithm URL according to the XML Signature specification (SHA1 is used by default)

#### DISCOVERY_SERVICE_LABEL

**Type:** Optional[str]

#### DISCOVERY_SERVICE_URL

**Type:** Optional[str]

#### DISPLAY_NAME

**Type:** str

Service provider display name (required by CoC)

#### ENABLE_SINGLE_LOGOUT

**Type:** bool

#### IDENTITY_PROVIDER_LABEL

**Type:** Optional[str]

#### IDENTITY_PROVIDER_URL

**Type:** Optional[str]

#### IDP_METADATA_LOCAL

**Type:** List[str]

IdPs metadata XML files stored locally

#### IDP_METADATA_REMOTE

**Type:** List[str]

IdPs metadata XML files stored remotely

#### KEY_FILE

**Type:** str

PEM formatted certificate key file

#### LOGOUT_REQUESTS_SIGNED

**Type:** str

Indicates if the entity will sign the logout requests

#### LOG_FILE

**Type:** str

Empty to disable logging SAML2-related stuff to file

#### LOG_LEVEL

**Type:** str

Log level for SAML2

#### MANAGEMENT_URL

**Type:** str

The endpoint for user details management.

#### NAME

**Type:** str

Name used for assigning the registration method to the user

#### NAMEID_FORMAT

**Type:** Optional[str]

Identified NameID format to use. None means default, empty string ("") disables addition of entity

#### OPTIONAL_ATTRIBUTES

**Type:** List[str]

SAML attributes that may be useful to have but not required

#### ORGANIZATION

**Type:** Mapping[str, Any]

Organization responsible for the service (you can set multilanguage information here)

#### PRIVACY_STATEMENT_URL

**Type:** str

URL with privacy statement (required by CoC)

#### REGISTRATION_AUTHORITY

**Type:** str

Registration authority required by mdpi

#### REGISTRATION_INSTANT

**Type:** str

Registration instant time required by mdpi

#### REGISTRATION_POLICY

**Type:** str

Registration policy required by mdpi

#### REQUIRED_ATTRIBUTES

**Type:** List[str]

SAML attributes that are required to identify a user

#### SAML_ATTRIBUTE_MAPPING

**Type:** Mapping[str, str]

Mapping between SAML attributes and User fields

#### SIGNATURE_ALGORITHM

**Type:** Optional[str]

Identifies the Signature algorithm URL according to the XML Signature specification (SHA1 is used by default)

#### XMLSEC_BINARY

**Type:** str

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

**Type:** bool

Enable eduTEAMS synchronization with remote Waldur.

#### REMOTE_EDUTEAMS_CLIENT_ID

**Type:** str

ID of application used for OAuth authentication.

#### REMOTE_EDUTEAMS_ENABLED

**Type:** bool

Enable remote eduTEAMS extension.

#### REMOTE_EDUTEAMS_REFRESH_TOKEN

**Type:** str

Token is used to authenticate against user info endpoint.

#### REMOTE_EDUTEAMS_SECRET

**Type:** str

Application secret key.

#### REMOTE_EDUTEAMS_SSH_API_PASSWORD

**Type:** str

Password for SSH API URL

#### REMOTE_EDUTEAMS_SSH_API_URL

**Type:** str

API URL SSH keys

#### REMOTE_EDUTEAMS_SSH_API_USERNAME

**Type:** str

Username for SSH API URL

#### REMOTE_EDUTEAMS_TOKEN_URL

**Type:** str

The token endpoint is used to obtain tokens.

#### REMOTE_EDUTEAMS_USERINFO_URL

**Type:** str

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

**Type:** timedelta

Max age of secure token for media download.

#### AUTHENTICATION_METHODS

**Type:** List[str]

List of enabled authentication methods.

#### BACKEND_FIELDS_EDITABLE

**Type:** bool

Allows to control /admin writable fields. If this flag is disabled it is impossible to edit any field that corresponds to backend value via /admin. Such restriction allows to save information from corruption.

#### COURSE_ACCOUNT_TOKEN_CLIENT_ID

**Type:** str

Client ID to get access token for course account.

#### COURSE_ACCOUNT_TOKEN_SECRET

**Type:** str

Client secret to get access for course account.

#### COURSE_ACCOUNT_TOKEN_URL

**Type:** str

Webhook URL for getting token for further course account management.

#### COURSE_ACCOUNT_URL

**Type:** str

Webhook URL for course account management.

#### COURSE_ACCOUNT_USE_API

**Type:** bool

Send course account creation and deletion requests to API.

#### CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION

**Type:** bool

Enables generation of the first project on organization creation.

#### EMAIL_CHANGE_MAX_AGE

**Type:** timedelta

Max age of change email request.

#### ENABLE_ACCOUNTING_START_DATE

**Type:** bool

Allows to enable accounting for organizations using value of accounting_start_date field.

#### ENABLE_PROJECT_KIND_COURSE

**Type:** bool

Enable course kind for projects.

#### EXTENSIONS_AUTOREGISTER

**Type:** bool

Defines whether extensions should be automatically registered.

#### EXTERNAL_LINKS

**Type:** List[ExternalLink]

Render external links in dropdown in header. Each item should be object with label and url fields. For example: {"label": "Helpdesk", "url": "`https://example.com/`"}

#### HOMEPORT_SENTRY_DSN

**Type:** Optional[str]

Sentry Data Source Name for Waldur HomePort project.

#### HOMEPORT_SENTRY_ENVIRONMENT

**Type:** str

Sentry environment name for Waldur Homeport.

#### HOMEPORT_SENTRY_TRACES_SAMPLE_RATE

**Type:** float

Percentage of transactions sent to Sentry for tracing.

#### HTTP_CHUNK_SIZE

**Type:** int

Chunk size for resource fetching from backend API. It is needed in order to avoid too long HTTP request error.

#### INVITATIONS_ENABLED

**Type:** bool

Allows to disable invitations feature.

#### INVITATION_CIVIL_NUMBER_LABEL

**Type:** str

Custom label for civil number field in invitation creation dialog.

#### INVITATION_CREATE_MISSING_USER

**Type:** bool

Allow to create FreeIPA user using details specified in invitation if user does not exist yet.

#### INVITATION_LIFETIME

**Type:** timedelta

Defines for how long invitation remains valid.

#### INVITATION_MAX_AGE

**Type:** Optional[timedelta]

Max age of invitation token. It is used in approve and reject actions.

#### INVITATION_USE_WEBHOOKS

**Type:** bool

Allow sending of webhooks instead of sending of emails.

#### INVITATION_WEBHOOK_TOKEN_CLIENT_ID

**Type:** str

Client ID to get access token from Keycloak.

#### INVITATION_WEBHOOK_TOKEN_SECRET

**Type:** str

Client secret to get access token from Keycloak.

#### INVITATION_WEBHOOK_TOKEN_URL

**Type:** str

Keycloak URL to get access token.

#### INVITATION_WEBHOOK_URL

**Type:** str

Webhook URL for sending invitations.

#### LOCAL_IDP_LABEL

**Type:** str

The label of local auth.

#### LOCAL_IDP_MANAGEMENT_URL

**Type:** str

The URL for management of local user details.

#### LOCAL_IDP_NAME

**Type:** str

The name of local auth.

#### LOCAL_IDP_PROTECTED_FIELDS

**Type:** List[str]

The list of protected fields for local IdP.

#### LOGGING_REPORT_DIRECTORY

**Type:** str

Directory where log files are located.

#### LOGGING_REPORT_INTERVAL

**Type:** timedelta

Files older that specified interval are filtered out.

#### MASTERMIND_URL

**Type:** str

It is used for rendering callback URL in MasterMind.

#### MATOMO_SITE_ID

**Type:** Optional[int]

Site ID is used by Matomo analytics application.

#### MATOMO_URL_BASE

**Type:** Optional[str]

URL base is used by Matomo analytics application.

#### NOTIFICATIONS_PROFILE_CHANGES

**Type:** Mapping[str, Any]

Configure notifications about profile changes of organization owners.

#### NOTIFICATION_SUBJECT

**Type:** str

It is used as a subject of email emitted by event logging hook.

#### OECD_FOS_2007_CODE_MANDATORY

**Type:** bool

Field oecd_fos_2007_code must be required for project.

#### ONLY_STAFF_CAN_INVITE_USERS

**Type:** bool

Allow to limit invitation management to staff only.

#### PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS

**Type:** List[str]

List of authentication methods for which a manual update of user details is not allowed.

#### REQUEST_HEADER_IMPERSONATED_USER_UUID

**Type:** str

The request header, which contains the user UUID of the user to be impersonated.

#### RESPONSE_HEADER_IMPERSONATOR_UUID

**Type:** str

The response header, which contains the UUID of the user who requested the impersonation.

#### SELLER_COUNTRY_CODE

**Type:** Optional[str]

Specifies seller legal or effective country of registration or residence as an ISO 3166-1 alpha-2 country code. It is used for computing VAT charge rate.

#### SERVICE_ACCOUNT_TOKEN_CLIENT_ID

**Type:** str

Client ID to get access token for service account.

#### SERVICE_ACCOUNT_TOKEN_SECRET

**Type:** str

Client secret to get access for service account.

#### SERVICE_ACCOUNT_TOKEN_URL

**Type:** str

Webhook URL for getting token for further service account management.

#### SERVICE_ACCOUNT_URL

**Type:** str

Webhook URL for service account management.

#### SERVICE_ACCOUNT_USE_API

**Type:** bool

Send service account creation and deletion requests to API.

#### SUBNET_BLACKLIST

**Type:** List[str]

List of IP ranges that are blocked for the SDK client.

#### SUPPORT_PORTAL_URL

**Type:** str

Support portal URL is rendered as a shortcut on dashboard

#### TOKEN_LIFETIME

**Type:** timedelta

Defines for how long user token should remain valid if there was no action from user.

#### TRANSLATION_DOMAIN

**Type:** str

Identifier of translation domain applied to current deployment.

#### USER_MANDATORY_FIELDS

**Type:** List[str]

List of user profile attributes that would be required for filling in HomePort. Note that backend will not be affected. If a mandatory field is missing in profile, a profile edit view will be forced upon user on any HomePort logged in action. Possible values are: description, email, full_name, job_title, organization, phone_number

#### USER_REGISTRATION_HIDDEN_FIELDS

**Type:** List[str]

List of user profile attributes that would be concealed on registration form in HomePort. Possible values are: job_title, registration_method, phone_number

#### USE_ATOMIC_TRANSACTION

**Type:** bool

Wrap action views in atomic transaction.

#### VALIDATE_INVITATION_EMAIL

**Type:** bool

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

**Type:** bool

Enable HPC-specific hooks in Waldur deployment

#### EXTERNAL_AFFILIATIONS

**Type:** List[str]

List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to external organization.

#### EXTERNAL_CUSTOMER_UUID

**Type:** str

UUID of a Waldur organization (aka customer) where new external users would be added

#### EXTERNAL_EMAIL_PATTERNS

**Type:** List[str]

List of user email patterns (as regex) that define if the user belongs to external organization.

#### EXTERNAL_LIMITS

**Type:** Mapping[str, Any]

Overrided default values for SLURM offering to be created for users belonging to external organization.

#### INTERNAL_AFFILIATIONS

**Type:** List[str]

List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to internal organization.

#### INTERNAL_CUSTOMER_UUID

**Type:** str

UUID of a Waldur organization (aka customer) where new internal users would be added

#### INTERNAL_EMAIL_PATTERNS

**Type:** List[str]

List of user email patterns (as regex) that define if the user belongs to internal organization.

#### INTERNAL_LIMITS

**Type:** Mapping[str, Any]

Overrided default values for SLURM offering to be created for users belonging to internal organization.

#### OFFERING_UUID

**Type:** str

UUID of a Waldur SLURM offering, which will be used for creating allocations for users

#### PLAN_UUID

**Type:** str

UUID of a Waldur SLURM offering plan, which will be used for creating allocations for users

### WALDUR_OPENPORTAL plugin

Default value:

```python
WALDUR_OPENPORTAL = {'DEFAULT_LIMITS': {'NODE': 1000}, 'ENABLED': False}
```

#### DEFAULT_LIMITS

**Type:** Mapping[str, int]

Default limits of account that are set when OpenPortal account is provisioned.

#### ENABLED

**Type:** bool

Enable support for OpenPortal plugin in a deployment

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

**Type:** bool

If true, customer users would be offered actions for accessing OpenStack console

#### ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION

**Type:** bool

If true, allow connecting of instances directly to external networks

#### DEFAULT_BLACKLISTED_USERNAMES

**Type:** List[str]

Usernames that cannot be created by Waldur in OpenStack

#### DEFAULT_SECURITY_GROUPS

**Type:** `Tuple[dict[str, str | tuple[dict[str, str | int], ...]], ...]`

Default security groups and rules created in each of the provisioned OpenStack tenants

#### MAX_CONCURRENT_PROVISION

**Type:** Mapping[str, int]

Maximum parallel executions of provisioning operations for OpenStack resources

#### REQUIRE_AVAILABILITY_ZONE

**Type:** bool

If true, specification of availability zone during provisioning will become mandatory

#### SUBNET

**Type:** Mapping[str, str]

Default allocation pool for auto-created internal network

#### TENANT_CREDENTIALS_VISIBLE

**Type:** bool

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

**Type:** Mapping[str, str]

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

**Type:** str

Prefix for SLURM account name corresponding to Waldur allocation

#### CUSTOMER_PREFIX

**Type:** str

Prefix for SLURM account name corresponding to Waldur organization.

#### DEFAULT_LIMITS

**Type:** Mapping[str, int]

Default limits of account that are set when SLURM account is provisioned.

#### ENABLED

**Type:** bool

Enable support for SLURM plugin in a deployment

#### PRIVATE_KEY_PATH

**Type:** str

Path to private key file used as SSH identity file for accessing SLURM master.

#### PROJECT_PREFIX

**Type:** str

Prefix for SLURM account name corresponding to Waldur project.

### WALDUR_USER_ACTIONS plugin

Default value:

```python
WALDUR_USER_ACTIONS = {'CLEANUP_EXECUTION_HISTORY_DAYS': 90,
 'DEFAULT_SILENCE_DURATION_DAYS': 7,
 'ENABLED': False,
 'HIGH_URGENCY_NOTIFICATION_THRESHOLD': 1,
 'MAX_ACTIONS_PER_USER': 100,
 'NOTIFICATION_ENABLED': False}
```

#### CLEANUP_EXECUTION_HISTORY_DAYS

**Type:** int

Number of days to keep action execution history.

#### DEFAULT_SILENCE_DURATION_DAYS

**Type:** int

Default number of days to silence actions when no duration is specified.

#### ENABLED

**Type:** bool

Enable the user actions notification system.

#### HIGH_URGENCY_NOTIFICATION_THRESHOLD

**Type:** int

Number of high urgency actions that trigger immediate notification.

#### MAX_ACTIONS_PER_USER

**Type:** int

Maximum number of actions to store per user.

#### NOTIFICATION_ENABLED

**Type:** bool

Enable daily digest notifications for user actions.

### Other variables

#### DEFAULT_FROM_EMAIL

**Type:** str, **default value:** webmaster@localhost

Default email address to use for automated correspondence from Waldur.

#### DEFAULT_REPLY_TO_EMAIL

**Type:** str

Default email address to use for email replies.

#### EMAIL_HOOK_FROM_EMAIL

**Type:** str

Alternative email address to use for email hooks.

#### IMPORT_EXPORT_USE_TRANSACTIONS

**Type:** bool, **default value:** True

Controls if resource importing should use database transactions. Using transactions makes imports safer as a failure during import won't import only part of the data set.

#### IPSTACK_ACCESS_KEY

**Type:** Optional[str]

Unique authentication key used to gain access to the ipstack API.

#### LANGUAGES

**Type:** List[tuple[str, str]], **default value:** [('en', 'English'), ('et', 'Eesti')]

The list is a list of two-tuples in the format (language code, language name) – for example, ('ja', 'Japanese').

#### LANGUAGE_CODE

**Type:** str, **default value:** en

Represents the name of a default language.

#### VERIFY_WEBHOOK_REQUESTS

**Type:** bool, **default value:** True

When webook is processed, requests verifies SSL certificates for HTTPS requests, just like a web browser.

## Dynamic options

### Branding

#### SITE_NAME

**Type:** str

**Default value:** Waldur

Human-friendly name of the Waldur deployment.

#### SHORT_PAGE_TITLE

**Type:** str

**Default value:** Waldur

It is used as prefix for page title.

#### FULL_PAGE_TITLE

**Type:** str

**Default value:** `Waldur | Cloud Service Management`

It is used as default page title if it's not specified explicitly.

#### SITE_DESCRIPTION

**Type:** str

**Default value:** Your single pane of control for managing projects, teams and resources in a self-service manner.

Description of the Waldur deployment.

#### HOMEPORT_URL

**Type:** str

**Default value:** <https://example.com/>

It is used for rendering callback URL in HomePort

#### RANCHER_USERNAME_INPUT_LABEL

**Type:** str

**Default value:** Username

Label for the username field in Rancher external user resource access management.

#### DISCLAIMER_AREA_TEXT

**Type:** text_field

Text content rendered in the disclaimer area below the footer.

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

**Default value:** EUR

It is used in marketplace order details and invoices for currency formatting.

#### MARKETPLACE_LANDING_PAGE

**Type:** str

**Default value:** Marketplace

Marketplace landing page title.

#### MARKETPLACE_LAYOUT_MODE

**Type:** choice_field

**Default value:** classic

Default marketplace layout mode.

#### MARKETPLACE_CARD_STYLE

**Type:** choice_field

**Default value:** detailed

Default marketplace offering card style.

#### COUNTRIES

**Type:** country_list_field

**Default value:** ['AL', 'AT', 'BA', 'BE', 'BG', 'CH', 'CY', 'CZ', 'DE', 'DK', 'EE', 'ES', 'EU', 'FI', 'FR', 'GB', 'GE', 'GR', 'HR', 'HU', 'IE', 'IS', 'IT', 'LT', 'LU', 'LV', 'MC', 'MK', 'MT', 'NL', 'NO', 'PL', 'PT', 'RO', 'RS', 'SE', 'SI', 'SK', 'UA']

It is used in organization creation dialog in order to limit country choices to predefined set.

### Marketplace visibility & access

#### ANONYMOUS_USER_CAN_VIEW_OFFERINGS

**Type:** bool

**Default value:** True

Allow anonymous users to see shared offerings in active, paused and archived states

#### ANONYMOUS_USER_CAN_VIEW_PLANS

**Type:** bool

**Default value:** True

Allow anonymous users to see plans

#### RESTRICTED_OFFERING_VISIBILITY_MODE

**Type:** choice_field

**Default value:** show_all

Controls offering visibility for regular users. 'show_all': Show all shared offerings (current behavior). 'show_restricted_disabled': Show all but mark inaccessible as disabled. 'hide_inaccessible': Hide offerings user cannot access. 'require_membership': Hide all unless user belongs to an organization/project.

#### SHOW_OFFERING_COVER_IMAGE

**Type:** bool

Show offering cover image as a banner above the name on the offering page.

#### ENFORCE_USER_CONSENT_FOR_OFFERINGS

**Type:** bool

If True, users must have active consent to access offerings that have active Terms of Service.

#### ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS

**Type:** bool

If True, service providers only see offering users whose profiles have all exposed attributes filled (per OfferingUserAttributeConfig).

#### ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT

**Type:** bool

If true, service provider owners and managers can manage offering lifecycle (activate, pause, unpause, archive, draft, delete) without staff approval.

### Marketplace notifications

#### NOTIFY_STAFF_ABOUT_APPROVALS

**Type:** bool

If true, users with staff role are notified when request for order approval is generated

#### NOTIFY_ABOUT_RESOURCE_CHANGE

**Type:** bool

**Default value:** True

If true, notify users about resource changes from Marketplace perspective. Can generate duplicate events if plugins also log

#### DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE

**Type:** bool

**Default value:** True

Disable only resource update events.

#### ENABLE_STALE_RESOURCE_NOTIFICATIONS

**Type:** bool

Enable reminders to owners about resources of shared offerings that have not generated any cost for the last 3 months.

### Offerings & orders

#### THUMBNAIL_SIZE

**Type:** str

**Default value:** 120x120

Size of the thumbnail to generate when screenshot is uploaded for an offering.

#### DISABLED_OFFERING_TYPES

**Type:** multiple_choice_field

List of offering types disabled for creation and selection.

#### ENABLE_ORDER_START_DATE

**Type:** bool

Allow setting start date to control when resource creation order is processed.

### Marketplace development

#### ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND

**Type:** bool

Enable mock returns for the service account service

#### ENABLE_MOCK_COURSE_ACCOUNT_BACKEND

**Type:** bool

Enable mock returns for the course account service

### Project

#### PROJECT_END_DATE_MANDATORY

**Type:** bool

If true, project end date field becomes mandatory when creating or updating projects.

### Telemetry

#### TELEMETRY_URL

**Type:** str

**Default value:** <https://telemetry.waldur.com/>

URL for sending telemetry data.

#### TELEMETRY_VERSION

**Type:** int

**Default value:** 1

Telemetry service version.

### Custom Scripts

#### SCRIPT_RUN_MODE

**Type:** choice_field

**Default value:** docker

Type of jobs deployment. Valid values: "docker" for simple docker deployment, "k8s" for Kubernetes-based one

#### DOCKER_CLIENT

**Type:** dict_field

**Default value:** {'base_url': 'unix:///var/run/docker.sock'}

Options for docker client. See also: <https://docker-py.readthedocs.io/en/stable/client.html#docker.client.DockerClient>

#### DOCKER_RUN_OPTIONS

**Type:** dict_field

**Default value:** {'mem_limit': '512m'}

Options for docker runtime. See also: <https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.ContainerCollection.run>

#### DOCKER_SCRIPT_DIR

**Type:** str

Path to folder on executor machine where to create temporary submission scripts. If None, uses OS-dependent location. OS X users, see <https://github.com/docker/for-mac/issues/1532>

#### DOCKER_REMOVE_CONTAINER

**Type:** bool

**Default value:** True

Remove Docker container after script execution

#### DOCKER_IMAGES

**Type:** dict_field

**Default value:** {'python': {'image': 'python:3.12-alpine', 'command': 'python'}, 'shell': {'image': 'alpine:3', 'command': 'sh'}, 'ansible': {'image': 'alpine/ansible:2.18.6', 'command': 'ansible-playbook'}}

Key is command to execute script, value is a dictionary of image name and command.

#### DOCKER_VOLUME_NAME

**Type:** str

**Default value:** waldur-docker-compose_waldur_script_launchzone

A name of the shared volume to store scripts

#### K8S_NAMESPACE

**Type:** str

**Default value:** default

Kubernetes namespace where jobs will be executed

#### K8S_CONFIG_PATH

**Type:** str

**Default value:** ~/.kube/config

Path to Kubernetes configuration file

#### K8S_JOB_TIMEOUT

**Type:** int

**Default value:** 1800

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

**Default value:** 60

How many minutes before scheduled maintenance users should be notified.

#### MAINTENANCE_ANNOUNCEMENT_NOTIFY_SYSTEM

**Type:** multiple_choice_field

**Default value:** ['AdminAnnouncement']

How maintenance notifications are delivered.

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

**Type:** choice_field

**Default value:** dark

Style of sidebar.

#### FONT_FAMILY

**Type:** choice_field

**Default value:** Inter

Font family used in the UI.

#### BRAND_COLOR

**Type:** color_field

**Default value:** #307300

Brand color is used for button background.

#### DISABLE_DARK_THEME

**Type:** bool

Toggler to disable dark theme.

### Login page

#### LOGIN_PAGE_LAYOUT

**Type:** choice_field

**Default value:** split-screen

Login page layout style.

#### LOGIN_PAGE_VIDEO_URL

**Type:** url_field

Video URL for the video-background login page layout. Supports MP4 format. Leave empty to use default sample video.

#### LOGIN_PAGE_STATS

**Type:** json_list_field

Stats displayed in the Stats login page layout. List of objects with 'value' and 'label' keys, e.g., [{'value': '10K+', 'label': 'Active Users'}, {'value': '99.9%', 'label': 'Uptime'}].

#### LOGIN_PAGE_CAROUSEL_SLIDES

**Type:** json_list_field

Carousel slides displayed in the Carousel login page layout. List of objects with 'title' and 'subtitle' keys, e.g., [{'title': 'Welcome', 'subtitle': 'Get started with our platform'}].

#### LOGIN_PAGE_NEWS

**Type:** json_list_field

News items displayed in the News login page layout. List of objects with 'date', 'title', 'description', and 'tag' keys. Supported tags: Feature, Update, Security, Announcement, Maintenance. Example: [{'date': 'Jan 2025', 'title': 'New Feature', 'description': 'Description here', 'tag': 'Feature'}].

### Images

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

#### LOGIN_LOGO_MULTILINGUAL

**Type:** multilingual_image_field

Language-specific login logos. Dict mapping language codes to image paths, e.g., {'de': 'path/to/german_logo.png'}. Falls back to LOGIN_LOGO if requested language not found.

#### FAVICON

**Type:** image_field

A custom favicon .png image file

#### OFFERING_LOGO_PLACEHOLDER

**Type:** image_field

Default logo for offering

#### KEYCLOAK_ICON

**Type:** image_field

A custom PNG icon for Keycloak login button

#### DISCLAIMER_AREA_LOGO

**Type:** image_field

The logo image rendered in the disclaimer area below the footer.

### Service desk integration settings

#### WALDUR_SUPPORT_ENABLED

**Type:** bool

**Default value:** True

Toggler for support plugin.

#### WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE

**Type:** choice_field

**Default value:** atlassian

Type of support backend.

#### WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE

**Type:** bool

**Default value:** True

Toggler for request type displaying

### Atlassian settings

#### ATLASSIAN_API_URL

**Type:** url_field

**Default value:** <https://example.com/>

Atlassian API server URL

#### ATLASSIAN_USERNAME

**Type:** str

**Default value:** USERNAME

Username for access user

#### ATLASSIAN_PASSWORD

**Type:** secret_field

**Default value:** PASSWORD

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

**Default value:** Bearer

OAuth 2.0 Token Type

#### ATLASSIAN_PROJECT_ID

**Type:** str

Service desk ID or key

#### ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE

**Type:** str

**Default value:** Service Request

Issue type used for request-based item processing.

#### ATLASSIAN_EXCLUDED_ATTACHMENT_TYPES

**Type:** str

Comma-separated list of file extenstions not allowed for attachment.

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

**Default value:** Impact

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

**Default value:** Original Reporter

Reporter field name

#### ATLASSIAN_CALLER_FIELD

**Type:** str

**Default value:** Caller

Caller field name

#### ATLASSIAN_SLA_FIELD

**Type:** str

**Default value:** Time to first response

SLA field name

#### ATLASSIAN_LINKED_ISSUE_TYPE

**Type:** str

**Default value:** Relates

Type of linked issue field name

#### ATLASSIAN_SATISFACTION_FIELD

**Type:** str

**Default value:** Customer satisfaction

Customer satisfaction field name

#### ATLASSIAN_REQUEST_FEEDBACK_FIELD

**Type:** str

**Default value:** Request feedback

Request feedback field name

#### ATLASSIAN_TEMPLATE_FIELD

**Type:** str

Template field name

#### ATLASSIAN_WALDUR_BACKEND_ID_FIELD

**Type:** str

**Default value:** customfield_10200

Waldur backend ID custom field ID (fallback when field lookup by name fails)

#### ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED

**Type:** bool

**Default value:** True

Should extra issue field mappings be applied

#### ATLASSIAN_SHARED_USERNAME

**Type:** bool

Is Service Desk username the same as in Waldur

#### ATLASSIAN_VERIFY_SSL

**Type:** bool

**Default value:** True

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

**Type:** choice_field

**Default value:** email

Type of a comment.

#### ZAMMAD_COMMENT_MARKER

**Type:** str

**Default value:** Created by Waldur

Marker for comment. Used for separating comments made via Waldur from natively added comments.

#### ZAMMAD_COMMENT_PREFIX

**Type:** str

**Default value:** User: {name}

Comment prefix with user info.

#### ZAMMAD_COMMENT_COOLDOWN_DURATION

**Type:** int

**Default value:** 5

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

**Default value:** 1

Duration in seconds of delay between pull user attempts.

#### SMAX_TIMES_TO_PULL

**Type:** int

**Default value:** 10

The maximum number of attempts to pull user from backend.

#### SMAX_CREATION_SOURCE_NAME

**Type:** str

Creation source name.

#### SMAX_VERIFY_SSL

**Type:** bool

**Default value:** True

Toggler for SSL verification

### Proposal settings

#### PROPOSAL_REVIEW_DURATION

**Type:** int

**Default value:** 7

Review duration in days.

#### REVIEWER_PROFILES_ENABLED

**Type:** bool

**Default value:** True

Enable reviewer profile management features.

#### COI_DETECTION_ENABLED

**Type:** bool

**Default value:** True

Enable conflict of interest detection features.

#### COI_DISCLOSURE_REQUIRED

**Type:** bool

Require reviewers to submit COI disclosure before reviewing proposals.

#### AUTOMATED_MATCHING_ENABLED

**Type:** bool

**Default value:** True

Enable automated reviewer-proposal matching algorithms.

#### COI_COAUTHORSHIP_LOOKBACK_YEARS

**Type:** int

**Default value:** 5

Default number of years to look back for co-authorship COI detection.

#### COI_COAUTHORSHIP_THRESHOLD_PAPERS

**Type:** int

**Default value:** 2

Default number of co-authored papers to trigger a COI.

#### COI_INSTITUTIONAL_LOOKBACK_YEARS

**Type:** int

**Default value:** 3

Default number of years after leaving institution before COI expires.

### ORCID integration settings

#### ORCID_CLIENT_ID

**Type:** str

ORCID OAuth2 Client ID for reviewer profile integration.

#### ORCID_CLIENT_SECRET

**Type:** secret_field

ORCID OAuth2 Client Secret.

#### ORCID_REDIRECT_URI

**Type:** url_field

ORCID OAuth2 Redirect URI. Typically {HOMEPORT_URL}/orcid-callback/

#### ORCID_API_URL

**Type:** url_field

**Default value:** <https://pub.orcid.org/v3.0>

ORCID API Base URL. Use https://pub.sandbox.orcid.org/v3.0 for testing.

#### ORCID_AUTH_URL

**Type:** url_field

**Default value:** <https://orcid.org/oauth>

ORCID OAuth Authorization URL. Use https://sandbox.orcid.org/oauth for testing.

#### ORCID_SANDBOX_MODE

**Type:** bool

Use ORCID sandbox environment for testing. When enabled, uses sandbox URLs automatically.

### Publication API settings

#### SEMANTIC_SCHOLAR_API_KEY

**Type:** secret_field

Semantic Scholar API Key for publication imports. Optional but recommended for higher rate limits.

#### CROSSREF_MAILTO

**Type:** email_field

Email address for CrossRef API polite pool. Provides higher rate limits.

### Table settings

#### USER_TABLE_COLUMNS

**Type:** str

Comma-separated list of columns for users table.

### Localization

#### LANGUAGE_CHOICES

**Type:** str

**Default value:** en,et,lt,lv,ru,it,de,da,sv,es,fr,nb,ar,cs

List of enabled languages

### Authentication settings

#### AUTO_APPROVE_USER_TOS

**Type:** bool

Mark terms of services as approved for new users.

#### DEFAULT_IDP

**Type:** choice_field

Triggers authentication flow at once.

#### DEACTIVATE_USER_IF_NO_ROLES

**Type:** bool

Deactivate user if all roles are revoked (except staff/support)

#### OIDC_BLOCK_CREATION_OF_UNINVITED_USERS

**Type:** bool

If true, block creation of an account on OIDC login if user email is not provided or provided and is not in the list of one of the active invitations or matching active group invitation email patterns.

#### OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE

**Type:** text_field

**Default value:** Account creation is blocked for uninvited users.

The message to show when OIDC account creation is blocked for uninvited users.

#### OIDC_MATCHMAKING_BY_EMAIL

**Type:** bool

If true, when OIDC login fails to find a user by the primary lookup field, attempt a secondary lookup by email before creating a new user. On successful email match, the user's primary lookup field is updated to the OIDC claim value.

#### OIDC_ACCESS_TOKEN_ENABLED

**Type:** bool

If true, OIDC complete view returns access token instead of Waldur token

#### REMOTE_EDUTEAMS_REFRESH_TOKEN

**Type:** secret_field

Rotating OAuth2 refresh token for remote eduTEAMS API access. Automatically updated by the periodic token rotation task. If empty, falls back to REMOTE_EDUTEAMS_REFRESH_TOKEN from Django settings.

### Invitation settings

#### ENABLE_STRICT_CHECK_ACCEPTING_INVITATION

**Type:** bool

If true, user email in Waldur database and in invitatation must strictly match.

#### INVITATION_DISABLE_MULTIPLE_ROLES

**Type:** bool

Do not allow user to accept multiple roles within the same scope (project or organization) using invitation. When enabled, users can still accept invitations to different scopes but cannot have multiple roles in the same scope.

#### INVITATION_ALLOWED_FIELDS

**Type:** multiple_choice_field

**Default value:** ['full_name', 'organization', 'job_title']

Fields that can be provided in invitations for email personalization. These are NOT copied to user profile.

### User profile settings

#### DEFAULT_OFFERING_USER_ATTRIBUTES

**Type:** multiple_choice_field

**Default value:** ['username', 'full_name', 'email']

Default user attributes exposed to service providers (OfferingUser API) when no explicit config exists.

#### ENABLED_USER_PROFILE_ATTRIBUTES

**Type:** multiple_choice_field

**Default value:** ['phone_number', 'organization', 'job_title', 'affiliations']

List of enabled user profile attributes. Controls IdP sync and UI display.

#### MANDATORY_USER_ATTRIBUTES

**Type:** multiple_choice_field

List of user profile attributes that are mandatory.

#### ENFORCE_MANDATORY_USER_ATTRIBUTES

**Type:** bool

If True, users with incomplete mandatory attributes will be blocked from most API endpoints until they complete their profile.

### Data privacy settings

#### USER_DATA_ACCESS_LOGGING_ENABLED

**Type:** bool

Enable logging of user profile data access events for GDPR compliance.

#### USER_DATA_ACCESS_LOG_RETENTION_DAYS

**Type:** int

**Default value:** 90

Number of days to retain user data access logs before automatic cleanup.

#### USER_DATA_ACCESS_LOG_SELF_ACCESS

**Type:** bool

Log when users access their own profile data. Disabled by default to reduce log volume.

### FreeIPA settings

#### FREEIPA_ENABLED

**Type:** bool

Enable integration of identity provisioning in configured FreeIPA.

#### FREEIPA_HOSTNAME

**Type:** str

**Default value:** ipa.example.com

Hostname of FreeIPA server.

#### FREEIPA_USERNAME

**Type:** str

**Default value:** admin

Username of FreeIPA user with administrative privileges.

#### FREEIPA_PASSWORD

**Type:** secret_field

**Default value:** secret

Password of FreeIPA user with administrative privileges

#### FREEIPA_VERIFY_SSL

**Type:** bool

**Default value:** True

Validate TLS certificate of FreeIPA web interface / REST API

#### FREEIPA_USERNAME_PREFIX

**Type:** str

**Default value:** waldur_

Prefix to be appended to all usernames created in FreeIPA by Waldur

#### FREEIPA_GROUPNAME_PREFIX

**Type:** str

**Default value:** waldur_

Prefix to be appended to all group names created in FreeIPA by Waldur

#### FREEIPA_BLACKLISTED_USERNAMES

**Type:** list_field

**Default value:** ['root']

List of username that users are not allowed to select

#### FREEIPA_GROUP_SYNCHRONIZATION_ENABLED

**Type:** bool

**Default value:** True

Optionally disable creation of user groups in FreeIPA matching Waldur structure

### SCIM settings

#### SCIM_MEMBERSHIP_SYNC_ENABLED

**Type:** bool

Enable SCIM entitlement synchronization to external identity provider.

#### SCIM_API_URL

**Type:** str

Base URL of the SCIM API service.

#### SCIM_API_KEY

**Type:** secret_field

SCIM API key for X-API-Key header.

#### SCIM_URN_NAMESPACE

**Type:** str

URN namespace for SCIM entitlements.

### API token authentication

#### OIDC_AUTH_URL

**Type:** str

OIDC authorization endpoint URL. Reserved for future OAuth 2.0 authorization code flow integration.

#### OIDC_INTROSPECTION_URL

**Type:** str

RFC 7662 Token Introspection endpoint URL. Used to validate API bearer tokens. When a client sends Authorization: Bearer <token>, Waldur calls this endpoint to verify the token is active.

#### OIDC_CLIENT_ID

**Type:** str

Client ID for HTTP Basic authentication when calling the token introspection endpoint. Required together with OIDC_CLIENT_SECRET and OIDC_INTROSPECTION_URL.

#### OIDC_CLIENT_SECRET

**Type:** secret_field

Client secret for HTTP Basic authentication when calling the token introspection endpoint. Required together with OIDC_CLIENT_ID and OIDC_INTROSPECTION_URL.

#### OIDC_USER_FIELD

**Type:** str

**Default value:** username

Field name from the introspection response JSON used to identify the Waldur user. Common values: 'username', 'email', 'sub', 'client_id'. The value is matched against User.username.

#### OIDC_CACHE_TIMEOUT

**Type:** int

**Default value:** 300

Seconds to cache successful token introspection results. Reduces load on the introspection endpoint. Set to 0 to disable caching. Default: 300 (5 minutes).

#### OIDC_DEFAULT_LOGOUT_URL

**Type:** url_field

Default logout URL used as fallback when IdentityProvider does not have a logout_url set. This allows configuring a global logout endpoint for OIDC providers that don't expose end_session_endpoint in their discovery document.

#### WALDUR_AUTH_SOCIAL_ROLE_CLAIM

**Type:** str

OAuth/OIDC token claim name containing user roles for automatic staff/support assignment. If the claim contains 'staff', user gets is_staff=True. If it contains 'support', user gets is_support=True. Leave empty to disable role synchronization from identity provider.

### Onboarding settings

#### ONBOARDING_VALIDATION_METHODS

**Type:** multiple_choice_field

List of automatic validation methods available for this portal.

#### ONBOARDING_VERIFICATION_EXPIRY_HOURS

**Type:** int

**Default value:** 48

Number of hours after which onboarding verifications expire.

#### ONBOARDING_ARIREGISTER_BASE_URL

**Type:** url_field

**Default value:** <https://demo-ariregxmlv6.rik.ee/>

Base URL for Estonian Äriregister API endpoint.

#### ONBOARDING_ARIREGISTER_USERNAME

**Type:** str

Username for Estonian Äriregister API authentication.

#### ONBOARDING_ARIREGISTER_PASSWORD

**Type:** secret_field

Password for Estonian Äriregister API authentication.

#### ONBOARDING_ARIREGISTER_TIMEOUT

**Type:** int

**Default value:** 30

Timeout in seconds for Estonian Äriregister API requests.

#### ONBOARDING_WICO_API_URL

**Type:** url_field

**Default value:** <https://api.wirtschaftscompass.at/>

WirtschaftsCompass API server URL

#### ONBOARDING_WICO_TOKEN

**Type:** secret_field

WirtschaftsCompass API token

#### ONBOARDING_BOLAGSVERKET_API_URL

**Type:** url_field

**Default value:** <https://gw-accept2.api.bolagsverket.se/>

Sweden Business Register API server URL

#### ONBOARDING_BOLAGSVERKET_TOKEN_API_URL

**Type:** url_field

**Default value:** <https://portal-accept2.api.bolagsverket.se/>

Bolagsverket OAuth2 token server base URL

#### ONBOARDING_BOLAGSVERKET_CLIENT_ID

**Type:** str

Sweden Business Register API client identifier

#### ONBOARDING_BOLAGSVERKET_CLIENT_SECRET

**Type:** secret_field

Sweden Business Register API client secret

#### ONBOARDING_BREG_API_URL

**Type:** url_field

**Default value:** <https://data.brreg.no/>

Norway Business Register API server URL

### AI assistant settings

#### AI_ASSISTANT_NAME

**Type:** str

**Default value:** Waldur Assistant

Display name for the AI Assistant persona (e.g. 'Mari', 'Waldur Assistant').

#### AI_ASSISTANT_ENABLED

**Type:** bool

Enable AI Assistant feature and calls to the inference service.

#### AI_ASSISTANT_ENABLED_ROLES

**Type:** choice_field

**Default value:** disabled

Controls which user roles can access the AI Assistant. 'disabled': No role-based access. 'staff': Staff users only. 'staff_and_support': Staff and support users. 'all': All authenticated users.

#### AI_ASSISTANT_BACKEND_TYPE

**Type:** str

**Default value:** vllm

Type of AI Assistant backend. For example: vllm, openai, ollama.

#### AI_ASSISTANT_API_URL

**Type:** url_field

Base URL for AI Assistant service API.

#### AI_ASSISTANT_API_TOKEN

**Type:** secret_field

API key for authenticating with the AI Assistant service.

#### AI_ASSISTANT_MODEL

**Type:** str

**Default value:** qwen3.5-122b-nothinking

Name of the AI Assistant model to use for inference.

#### AI_ASSISTANT_SYSTEM_PROMPT_CUSTOM_INSTRUCTIONS

**Type:** text_field

Additional instructions injected into the AI Assistant system prompt. Use this for organisation-specific context, terminology, FAQ content, or behavioural guidelines. Supports {assistant_name} and {organization} placeholders. Overridden by the active SystemPrompt record when set.

#### AI_ASSISTANT_COMPLETION_KWARGS

**Type:** dict_field

Override keyword arguments merged on top of provider defaults for AI Assistant chat completion. Supported keys: temperature, top_p, top_k, max_tokens, max_completion_tokens, presence_penalty, frequency_penalty, repetition_penalty, stop, seed, reasoning_effort, extra_body. Leave empty to use provider defaults.

#### AI_ASSISTANT_TOKEN_LIMIT_DAILY

**Type:** int

**Default value:** -1

Default daily token limit (integer). -1 means unlimited.

#### AI_ASSISTANT_TOKEN_LIMIT_WEEKLY

**Type:** int

**Default value:** -1

Default weekly token limit (integer). -1 means unlimited.

#### AI_ASSISTANT_TOKEN_LIMIT_MONTHLY

**Type:** int

**Default value:** -1

Default monthly token limit (integer). -1 means unlimited.

#### AI_ASSISTANT_SESSION_RETENTION_DAYS

**Type:** int

**Default value:** 90

Number of days to retain AI Assistant sessions before automatic deletion. Set to -1 to disable automatic cleanup.

#### AI_ASSISTANT_HISTORY_LIMIT

**Type:** int

**Default value:** 50

Maximum number of past messages included in the AI Assistant context window.

#### AI_ASSISTANT_INJECTION_ALLOWLIST

**Type:** str

Comma-separated allowlist phrases that bypass injection detection.

### Software catalog general

#### SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES

**Type:** bool

**Default value:** True

Update existing packages during catalog refresh

#### SOFTWARE_CATALOG_CLEANUP_ENABLED

**Type:** bool

**Default value:** True

Enable automatic cleanup of old catalog data

#### SOFTWARE_CATALOG_RETENTION_DAYS

**Type:** int

**Default value:** 90

Number of days to retain old catalog versions

### Software catalog EESSI

#### SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED

**Type:** bool

Enable automated daily updates for EESSI software catalog

#### SOFTWARE_CATALOG_EESSI_VERSION

**Type:** str

EESSI catalog version to load (auto-detect if empty)

#### SOFTWARE_CATALOG_EESSI_API_URL

**Type:** str

**Default value:** <https://www.eessi.io/api_data/data/>

Base URL for EESSI API data

#### SOFTWARE_CATALOG_EESSI_INCLUDE_EXTENSIONS

**Type:** bool

**Default value:** True

Include extension packages (Python, R packages, etc.) from EESSI

### Software catalog Spack

#### SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED

**Type:** bool

Enable automated daily updates for Spack software catalog

#### SOFTWARE_CATALOG_SPACK_VERSION

**Type:** str

Spack catalog version to load (auto-detect if empty)

#### SOFTWARE_CATALOG_SPACK_DATA_URL

**Type:** str

**Default value:** <https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json>

URL for Spack repology.json data

### System Logging

#### SYSTEM_LOG_ENABLED

**Type:** bool

Enable storing system logs (API, Worker, Beat) in the database for staff viewing.

#### SYSTEM_LOG_MAX_ROWS_PER_SOURCE

**Type:** int

**Default value:** 5000

Maximum number of log rows to keep per source (api, worker, beat). Oldest rows are deleted when exceeded.

### Table Growth Monitoring

#### TABLE_GROWTH_MONITORING_ENABLED

**Type:** bool

**Default value:** True

Enable table growth monitoring to detect potential data leaks from bugs.

#### TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT

**Type:** int

**Default value:** 50

Alert if a table grows by more than this percentage in a week.

#### TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT

**Type:** int

**Default value:** 200

Alert if a table grows by more than this percentage in a month.

#### TABLE_GROWTH_RETENTION_DAYS

**Type:** int

**Default value:** 90

Number of days to retain table size history data.

#### TABLE_GROWTH_MIN_SIZE_BYTES

**Type:** int

**Default value:** 1048576

Minimum table size in bytes (default 1MB) to monitor. Smaller tables are ignored.

### User Actions

#### USER_ACTIONS_ENABLED

**Type:** bool

Enable user actions notification system.

#### USER_ACTIONS_PENDING_ORDER_HOURS

**Type:** int

**Default value:** 24

Hours before pending order becomes a user action item (1-168).

#### USER_ACTIONS_HIGH_URGENCY_NOTIFICATION

**Type:** bool

**Default value:** True

Send digest notification if user has high urgency actions.

#### USER_ACTIONS_NOTIFICATION_THRESHOLD

**Type:** int

**Default value:** 5

Send digest notification if user has more than N actions.

#### USER_ACTIONS_EXECUTION_RETENTION_DAYS

**Type:** int

**Default value:** 90

Number of days to keep action execution history.

#### USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS

**Type:** list_field

**Default value:** [30, 14, 7, 1]

Default reminder schedule (days before expiration) for expiring resources. Can be overridden per offering via plugin_options.resource_expiration_reminders.

### Arrow Integration

#### ARROW_AUTO_RECONCILIATION

**Type:** bool

Auto-apply compensations when Arrow validates billing

#### ARROW_SYNC_INTERVAL_HOURS

**Type:** int

**Default value:** 6

Billing sync interval in hours

#### ARROW_CONSUMPTION_SYNC_ENABLED

**Type:** bool

Enable real-time consumption sync from Arrow API

#### ARROW_CONSUMPTION_SYNC_INTERVAL_HOURS

**Type:** int

**Default value:** 1

Consumption sync interval in hours (default: hourly)

#### ARROW_BILLING_CHECK_INTERVAL_HOURS

**Type:** int

**Default value:** 6

Billing export check interval in hours for reconciliation

### SLURM Policy

#### SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS

**Type:** int

**Default value:** 90

Number of days to retain SLURM policy evaluation log entries before automatic cleanup.

### Usage Polling

#### USAGE_POLL_RECORD_RETENTION_MONTHS

**Type:** int

**Default value:** 3

Number of months to retain usage poll records before automatic cleanup.

### Identity Bridge

#### FEDERATED_IDENTITY_SYNC_ENABLED

**Type:** bool

Enable the Identity Bridge API for push-based ISD user attribute synchronization.

#### FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES

**Type:** multiple_choice_field

**Default value:** ['first_name', 'last_name', 'email', 'organization', 'affiliations']

User attributes settable via Identity Bridge.

#### FEDERATED_IDENTITY_DEACTIVATION_POLICY

**Type:** choice_field

**Default value:** any_isd_removed

When to deactivate a federated user.

### Project Digest

#### ENABLE_PROJECT_DIGEST

**Type:** bool

Enable project digest email notifications for organizations.

### SSH keys

#### SSH_KEY_ALLOWED_TYPES

**Type:** multiple_choice_field

**Default value:** ['ssh-ed25519', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521', 'ssh-rsa', 'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com']

List of allowed SSH key types. Empty list means all types are allowed.

#### SSH_KEY_MIN_RSA_KEY_SIZE

**Type:** int

**Default value:** 2048

Minimum allowed RSA key size in bits. Set to 0 to disable the check.

#### ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES

**Type:** bool

If true, a support ticket is created when a user adds or removes an SSH public key.

### Reporting

#### ENABLED_REPORTING_SCREENS

**Type:** multiple_choice_field

**Default value:** ['resource-usage', 'user-usage', 'quotas', 'usage-monitoring', 'usage-trends', 'organization-summary', 'project-detail', 'resources-geography', 'project-classification', 'usage-by-customer', 'usage-by-org-type', 'usage-by-creator', 'call-performance', 'review-progress', 'resource-demand', 'capacity', 'provider-overview', 'provider-revenue', 'provider-orders', 'provider-resources', 'provider-customers', 'provider-offerings', 'openstack-instances', 'offering-usage', 'user-analytics', 'user-demographics', 'user-organizations', 'user-affiliations', 'user-roles', 'growth', 'revenue', 'pricelist', 'orders', 'offering-costs', 'maintenance-overview', 'provisioning-stats']

Select which reporting screens should be visible to users. Uncheck to disable specific reports.

### Personal Access Tokens

#### PAT_ENABLED

**Type:** bool

Enable Personal Access Token authentication.

#### PAT_MAX_LIFETIME_DAYS

**Type:** int

**Default value:** 365

Maximum PAT lifetime in days.

#### PAT_MAX_TOKENS_PER_USER

**Type:** int

**Default value:** 20

Maximum number of active PATs per user.
