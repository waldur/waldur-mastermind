import datetime
from datetime import timedelta
from typing import Any

import saml2
from pydantic.v1 import BaseModel, Field
from saml2.entity_category.edugain import COC


class ExternalLink(BaseModel):
    label: str
    url: str


class WaldurCore(BaseModel):
    EXTENSIONS_AUTOREGISTER: bool = Field(
        True,
        description="Defines whether extensions should be automatically registered.",
    )
    RESPONSE_HEADER_IMPERSONATOR_UUID: str = Field(
        "X-impersonator-uuid",
        description="The response header, which contains the UUID "
        "of the user who requested the impersonation.",
    )
    REQUEST_HEADER_IMPERSONATED_USER_UUID: str = Field(
        "HTTP_X_IMPERSONATED_USER_UUID",
        description="The request header, which contains the user UUID "
        "of the user to be impersonated.",
    )
    AUTHENTICATION_METHODS: list[str] = Field(
        ["LOCAL_SIGNIN"], description="List of enabled authentication methods."
    )
    PASSKEY_RP_ID: str = Field(
        "",
        description="WebAuthn Relying Party ID: the bare registrable domain that "
        "passkeys are bound to, without scheme or port, e.g. 'waldur.example.com'. "
        "Has no default and cannot be derived from the request, because no "
        "deployment sets SECURE_PROXY_SSL_HEADER. Changing it orphans every "
        "credential already registered.",
    )
    PASSKEY_RP_NAME: str = Field(
        "",
        description="Human-readable Relying Party name shown by the authenticator "
        "during registration. Defaults to SITE_NAME when left empty.",
    )
    PASSKEY_ALLOWED_ORIGINS: list[str] = Field(
        [],
        description="Full origins the SPA may run WebAuthn ceremonies from, "
        "e.g. ['https://waldur.example.com']. Each must be subordinate to "
        "PASSKEY_RP_ID, and HTTPS outside localhost.",
    )
    PASSKEY_ENFORCED_FOR_STAFF: bool = Field(
        False,
        description="Require staff and support accounts to hold a passkey and "
        "to have satisfied it for the current session. Closes the paths that "
        "otherwise yield a privileged session without one: reading another "
        "user's raw API token, impersonation, the Django admin login form, "
        "and minting a personal access token. Enabling it logs every staff "
        "member out, because pre-existing tokens were issued without a "
        "passkey; run 'waldur revoke_unverified_staff_tokens' as part of the "
        "rollout.",
    )
    INVITATIONS_ENABLED: bool = Field(
        True, description="Allows to disable invitations feature."
    )
    VALIDATE_INVITATION_EMAIL: bool = Field(
        False, description="Ensure that invitation and user emails match."
    )
    TOKEN_LIFETIME: timedelta = Field(
        timedelta(hours=1),
        description="Defines for how long user token should remain valid if there was no action from user.",
    )
    INVITATION_LIFETIME: timedelta = Field(
        timedelta(weeks=1), description="Defines for how long invitation remains valid."
    )
    BACKEND_FIELDS_EDITABLE: bool = Field(
        True,
        description="Allows to control /admin writable fields. "
        "If this flag is disabled it is impossible to edit any field that corresponds to "
        "backend value via /admin. Such restriction allows to save information from corruption.",
    )
    CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION: bool = Field(
        False,
        description="Enables generation of the first project on organization creation.",
    )
    NOTIFICATIONS_PROFILE_CHANGES: dict[str, Any] = Field(
        {
            "FIELDS": ("email", "phone_number", "job_title"),
            "ENABLE_OPERATOR_OWNER_NOTIFICATIONS": False,
            "OPERATOR_NOTIFICATION_EMAILS": [],
        },
        description="Configure notifications about profile changes of organization owners.",
    )
    ENABLE_ACCOUNTING_START_DATE: bool = Field(
        False,
        description="Allows to enable accounting for organizations using value of accounting_start_date field.",
    )
    USE_ATOMIC_TRANSACTION: bool = Field(
        True, description="Wrap action views in atomic transaction."
    )
    NOTIFICATION_SUBJECT: str = Field(
        "Notifications from Waldur",
        description="It is used as a subject of email emitted by event logging hook.",
    )
    LOGGING_REPORT_DIRECTORY: str = Field(
        "/var/log/waldur", description="Directory where log files are located."
    )
    LOGGING_REPORT_INTERVAL: timedelta = Field(
        timedelta(days=7),
        description="Files older that specified interval are filtered out.",
    )
    HTTP_CHUNK_SIZE: int = Field(
        50,
        description="Chunk size for resource fetching from backend API. "
        "It is needed in order to avoid too long HTTP request error.",
    )
    ONLY_STAFF_CAN_INVITE_USERS: bool = Field(
        False, description="Allow to limit invitation management to staff only."
    )
    INVITATION_MAX_AGE: timedelta | None = Field(
        None,
        description="Max age of invitation token. It is used in approve and reject actions.",
    )
    INVITATION_CREATE_MISSING_USER: bool = Field(
        False,
        description="Allow to create FreeIPA user using details specified in invitation if user does not exist yet.",
    )
    INVITATION_USE_WEBHOOKS: bool = Field(
        False,
        description="Allow sending of webhooks instead of sending of emails.",
    )
    INVITATION_WEBHOOK_URL: str = Field(
        "", description="Webhook URL for sending invitations."
    )
    INVITATION_WEBHOOK_TOKEN_URL: str = Field(
        "", description="Keycloak URL to get access token."
    )
    INVITATION_WEBHOOK_TOKEN_CLIENT_ID: str = Field(
        "", description="Client ID to get access token from Keycloak."
    )
    INVITATION_WEBHOOK_TOKEN_SECRET: str = Field(
        "", description="Client secret to get access token from Keycloak."
    )
    SERVICE_ACCOUNT_TOKEN_URL: str = Field(
        "",
        description="Webhook URL for getting token for further service account management.",
    )
    SERVICE_ACCOUNT_URL: str = Field(
        "", description="Webhook URL for service account management."
    )
    SERVICE_ACCOUNT_TOKEN_CLIENT_ID: str = Field(
        "", description="Client ID to get access token for service account."
    )
    SERVICE_ACCOUNT_TOKEN_SECRET: str = Field(
        "", description="Client secret to get access for service account."
    )
    COURSE_ACCOUNT_TOKEN_URL: str = Field(
        "",
        description="Webhook URL for getting token for further course account management.",
    )
    COURSE_ACCOUNT_URL: str = Field(
        "", description="Webhook URL for course account management."
    )
    COURSE_ACCOUNT_TOKEN_CLIENT_ID: str = Field(
        "", description="Client ID to get access token for course account."
    )
    COURSE_ACCOUNT_TOKEN_SECRET: str = Field(
        "", description="Client secret to get access for course account."
    )
    PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS: list[str] = Field(
        [],
        description="List of authentication methods for which a manual update of user details is not allowed.",
    )
    ATTACHMENT_LINK_MAX_AGE: timedelta = Field(
        timedelta(hours=1), description="Max age of secure token for media download."
    )
    EMAIL_CHANGE_MAX_AGE: timedelta = Field(
        timedelta(days=1), description="Max age of change email request."
    )
    MASTERMIND_URL: str = Field(
        "",
        description="It is used for rendering callback URL in MasterMind.",
    )
    SELLER_COUNTRY_CODE: str | None = Field(
        None,
        description="Specifies seller legal or effective country of registration or residence as an "
        "ISO 3166-1 alpha-2 country code. It is used for computing VAT charge rate.",
    )
    TRANSLATION_DOMAIN: str = Field(
        "",
        description="Identifier of translation domain applied to current deployment.",
    )
    MATOMO_URL_BASE: str | None = Field(
        None,
        description="URL base is used by Matomo analytics application.",
    )
    MATOMO_SITE_ID: int | None = Field(
        None,
        description="Site ID is used by Matomo analytics application.",
    )
    SUPPORT_PORTAL_URL: str = Field(
        "", description="Support portal URL is rendered as a shortcut on dashboard"
    )
    EXTERNAL_LINKS: list[ExternalLink] = Field(
        [],
        description="Render external links in dropdown in header. "
        "Each item should be object with label and url fields. "
        'For example: {"label": "Helpdesk", "url": "`https://example.com/`"}',
    )
    USER_MANDATORY_FIELDS: list[str] = Field(
        ["first_name", "last_name", "email"],
        description="List of user profile attributes that would be required for filling in HomePort. "
        "Note that backend will not be affected. If a mandatory field is missing in profile, "
        "a profile edit view will be forced upon user on any HomePort logged in action. "
        "Possible values are: description, email, full_name, job_title, organization, phone_number",
    )
    USER_REGISTRATION_HIDDEN_FIELDS: list[str] = Field(
        [
            "registration_method",
            "job_title",
            "phone_number",
            "organization",
        ],
        description="List of user profile attributes that would be concealed on registration form in HomePort. "
        "Possible values are: job_title, registration_method, phone_number",
    )

    INVITATION_CIVIL_NUMBER_LABEL: str = Field(
        "",
        description="Custom label for civil number field in invitation creation dialog.",
    )

    HOMEPORT_SENTRY_DSN: str | None = Field(
        None, description="Sentry Data Source Name for Waldur HomePort project."
    )

    HOMEPORT_SENTRY_ENVIRONMENT: str = Field(
        "waldur-production",
        description="Sentry environment name for Waldur Homeport.",
    )

    HOMEPORT_SENTRY_TRACES_SAMPLE_RATE: float = Field(
        0.01,
        description="Percentage of transactions sent to Sentry for tracing.",
    )

    LOCAL_IDP_NAME: str = Field("Local DB", description="The name of local auth.")

    LOCAL_IDP_LABEL: str = Field("Local DB", description="The label of local auth.")

    LOCAL_IDP_MANAGEMENT_URL: str = Field(
        "", description="The URL for management of local user details."
    )

    LOCAL_IDP_PROTECTED_FIELDS: list[str] = Field(
        [],
        description="The list of protected fields for local IdP.",
    )

    OECD_FOS_2007_CODE_MANDATORY: bool = Field(
        False,
        description="Field oecd_fos_2007_code must be required for project.",
    )
    SERVICE_ACCOUNT_USE_API: bool = Field(
        False,
        description="Send service account creation and deletion requests to API.",
    )
    COURSE_ACCOUNT_USE_API: bool = Field(
        False,
        description="Send course account creation and deletion requests to API.",
    )

    ENABLE_PROJECT_KIND_COURSE: bool = Field(
        False,
        description="Enable course kind for projects.",
    )

    SUBNET_BLACKLIST: list[str] = Field(
        [
            "10.0.0.0/8",  # Private networks class A
            "172.16.0.0/12",  # Private networks class B
            "192.168.0.0/16",  # Private networks class C
            "169.254.0.0/16",  # Link-local address for private networks IPV4
            "127.0.0.0/8",  # Localhost loopback
            "::1/128",  # IPv6 localhost loopback
            "fc00::/7",  # Unique local address for private networks
            "fe80::/10",  # Link-local address for private networks IPv6
        ],
        description="List of IP ranges that are blocked for the SDK client.",
    )

    class Meta:
        public_settings: list[str] = [
            "MASTERMIND_URL",
            "AUTHENTICATION_METHODS",
            "INVITATIONS_ENABLED",
            "VALIDATE_INVITATION_EMAIL",
            "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS",
            "TRANSLATION_DOMAIN",
            "MATOMO_URL_BASE",
            "MATOMO_SITE_ID",
            "SUPPORT_PORTAL_URL",
            "EXTERNAL_LINKS",
            "USER_MANDATORY_FIELDS",
            "USER_REGISTRATION_HIDDEN_FIELDS",
            "INVITATION_CIVIL_NUMBER_LABEL",
            "HOMEPORT_SENTRY_DSN",
            "HOMEPORT_SENTRY_ENVIRONMENT",
            "HOMEPORT_SENTRY_TRACES_SAMPLE_RATE",
            "OECD_FOS_2007_CODE_MANDATORY",
            "INVITATION_USE_WEBHOOKS",
            "SERVICE_ACCOUNT_USE_API",
            "COURSE_ACCOUNT_USE_API",
            "ENABLE_PROJECT_KIND_COURSE",
        ]


class WaldurUserActions(BaseModel):
    """Configuration for user actions notification system."""

    ENABLED: bool = Field(
        False,
        description="Enable the user actions notification system.",
    )

    MAX_ACTIONS_PER_USER: int = Field(
        100,
        description="Maximum number of actions to store per user.",
    )

    DEFAULT_SILENCE_DURATION_DAYS: int = Field(
        7,
        description="Default number of days to silence actions when no duration is specified.",
    )

    NOTIFICATION_ENABLED: bool = Field(
        False,
        description="Enable daily digest notifications for user actions.",
    )

    HIGH_URGENCY_NOTIFICATION_THRESHOLD: int = Field(
        1,
        description="Number of high urgency actions that trigger immediate notification.",
    )

    CLEANUP_EXECUTION_HISTORY_DAYS: int = Field(
        90,
        description="Number of days to keep action execution history.",
    )


class WaldurAuthSocial(BaseModel):
    REMOTE_EDUTEAMS_TOKEN_URL: str = Field(
        "https://proxy.acc.researcher-access.org/OIDC/token",
        description="The token endpoint is used to obtain tokens.",
    )
    REMOTE_EDUTEAMS_REFRESH_TOKEN: str = Field(
        "", description="Token is used to authenticate against user info endpoint."
    )
    REMOTE_EDUTEAMS_USERINFO_URL: str = Field(
        "https://proxy.acc.researcher-access.org/api/userinfo",
        description="It allows to get user data based on userid aka CUID.",
    )
    REMOTE_EDUTEAMS_CLIENT_ID: str = Field(
        "", description="ID of application used for OAuth authentication."
    )
    REMOTE_EDUTEAMS_SECRET: str = Field("", description="Application secret key.")
    REMOTE_EDUTEAMS_ENABLED: bool = Field(
        False, description="Enable remote eduTEAMS extension."
    )
    REMOTE_EDUTEAMS_SSH_API_URL: str = Field("", description="API URL SSH keys")
    REMOTE_EDUTEAMS_SSH_API_USERNAME: str = Field(
        "", description="Username for SSH API URL"
    )
    REMOTE_EDUTEAMS_SSH_API_PASSWORD: str = Field(
        "", description="Password for SSH API URL"
    )
    ENABLE_EDUTEAMS_SYNC: bool = Field(
        False, description="Enable eduTEAMS synchronization with remote Waldur."
    )

    class Meta:
        public_settings: list[str] = [
            "REMOTE_EDUTEAMS_ENABLED",
            "ENABLE_EDUTEAMS_SYNC",
        ]


class WaldurHPC(BaseModel):
    ENABLED: bool = Field(
        False,
        description="Enable HPC-specific hooks in Waldur deployment",
    )
    INTERNAL_CUSTOMER_UUID: str = Field(
        "",
        description="UUID of a Waldur organization (aka customer) where new internal users would be added",
    )
    EXTERNAL_CUSTOMER_UUID: str = Field(
        "",
        description="UUID of a Waldur organization (aka customer) where new external users would be added",
    )
    INTERNAL_AFFILIATIONS: list[str] = Field(
        [],
        description="List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to internal organization.",
    )
    EXTERNAL_AFFILIATIONS: list[str] = Field(
        [],
        description="List of user affiliations (eduPersonScopedAffiliation fields) that define if the user belongs to external organization.",
    )
    INTERNAL_EMAIL_PATTERNS: list[str] = Field(
        [],
        description="List of user email patterns (as regex) that define if the user belongs to internal organization.",
    )
    EXTERNAL_EMAIL_PATTERNS: list[str] = Field(
        [],
        description="List of user email patterns (as regex) that define if the user belongs to external organization.",
    )
    INTERNAL_LIMITS: dict[str, Any] = Field(
        {},
        description="Overrided default values for SLURM offering to be created for users belonging to internal organization.",
    )
    EXTERNAL_LIMITS: dict[str, Any] = Field(
        {},
        description="Overrided default values for SLURM offering to be created for users belonging to external organization.",
    )
    OFFERING_UUID: str = Field(
        "",
        description="UUID of a Waldur SLURM offering, which will be used for creating allocations for users",
    )
    PLAN_UUID: str = Field(
        "",
        description="UUID of a Waldur SLURM offering plan, which will be used for creating allocations for users",
    )


class WaldurOpenPortal(BaseModel):
    ENABLED: bool = Field(
        False,
        description="Enable support for OpenPortal plugin in a deployment",
    )
    DEFAULT_LIMITS: dict[str, int] = Field(
        {
            "NODE": 1000,  # Measured unit is node-hours
        },
        description="Default limits of account that are set when OpenPortal account is provisioned.",
    )

    class Meta:
        public_settings: list[str] = [
            "ENABLED",
        ]


class WaldurSlurm(BaseModel):
    ENABLED: bool = Field(
        False,
        description="Enable support for SLURM plugin in a deployment",
    )
    CUSTOMER_PREFIX: str = Field(
        "waldur_customer_",
        description="Prefix for SLURM account name corresponding to Waldur organization.",
    )
    PROJECT_PREFIX: str = Field(
        "waldur_project_",
        description="Prefix for SLURM account name corresponding to Waldur project.",
    )
    ALLOCATION_PREFIX: str = Field(
        "waldur_allocation_",
        description="Prefix for SLURM account name corresponding to Waldur allocation",
    )
    PRIVATE_KEY_PATH: str = Field(
        "/etc/waldur/id_rsa",
        description="Path to private key file used as SSH identity file for accessing SLURM master.",
    )
    DEFAULT_LIMITS: dict[str, int] = Field(
        {
            "CPU": 16000,  # Measured unit is CPU-minutes
            "GPU": 400,  # Measured unit is GPU-minutes
            "RAM": 100000 * 2**10,  # Measured unit is MB-h
        },
        description="Default limits of account that are set when SLURM account is provisioned.",
    )


class WaldurPID(BaseModel):
    DATACITE: dict[str, str] = Field(
        {
            "REPOSITORY_ID": "",
            "PASSWORD": "",
            "PREFIX": "",
            "API_URL": "https://example.com",
            "PUBLISHER": "Waldur",
            "COLLECTION_DOI": "",
        },
        description="Settings for integration of Waldur with Datacite PID service. Collection DOI is used to aggregate generated DOIs.",
    )


class WaldurAuthSAML2(BaseModel):
    NAME: str = Field(
        "saml2",
        description="Name used for assigning the registration method to the user",
    )
    XMLSEC_BINARY: str = Field(
        "/usr/bin/xmlsec1", description="Full path to the xmlsec1 binary program"
    )
    ATTRIBUTE_MAP_DIR: str = Field(
        "/etc/waldur/saml2/attributemaps",
        description="Directory with attribute mapping",
    )
    DEBUG: bool = Field(
        False, description="Set to True to output debugging information"
    )
    IDP_METADATA_LOCAL: list[str] = Field(
        [], description="IdPs metadata XML files stored locally"
    )
    IDP_METADATA_REMOTE: list[str] = Field(
        [], description="IdPs metadata XML files stored remotely"
    )
    LOG_FILE: str = Field(
        "", description="Empty to disable logging SAML2-related stuff to file"
    )
    LOG_LEVEL: str = Field("INFO", description="Log level for SAML2")
    LOGOUT_REQUESTS_SIGNED: str = Field(
        "true", description="Indicates if the entity will sign the logout requests"
    )
    AUTHN_REQUESTS_SIGNED: str = Field(
        "true",
        description="Indicates if the authentication requests sent should be signed by default",
    )
    SIGNATURE_ALGORITHM: str | None = Field(
        None,
        description="Identifies the Signature algorithm URL according to the XML Signature specification (SHA1 is used by default)",
    )
    DIGEST_ALGORITHM: str | None = Field(
        None,
        description="Identifies the Message Digest algorithm URL according to the XML Signature specification (SHA1 is used by default)",
    )
    NAMEID_FORMAT: str | None = Field(
        None,
        description='Identified NameID format to use. None means default, empty string ("") disables addition of entity',
    )
    CERT_FILE: str = Field("", description="PEM formatted certificate chain file")
    KEY_FILE: str = Field("", description="PEM formatted certificate key file")
    REQUIRED_ATTRIBUTES: list[str] = Field(
        [], description="SAML attributes that are required to identify a user"
    )
    OPTIONAL_ATTRIBUTES: list[str] = Field(
        [], description="SAML attributes that may be useful to have but not required"
    )
    SAML_ATTRIBUTE_MAPPING: dict[str, str] = Field(
        {}, description="Mapping between SAML attributes and User fields"
    )
    ORGANIZATION: dict[str, Any] = Field(
        {},
        description="Organization responsible for the service (you can set multilanguage information here)",
    )
    CATEGORIES: list[str] = Field([COC], description="Links to the entity categories")
    PRIVACY_STATEMENT_URL: str = Field(
        "http://example.com/privacy-policy/",
        description="URL with privacy statement (required by CoC)",
    )
    DISPLAY_NAME: str = Field(
        "Service provider display name",
        description="Service provider display name (required by CoC)",
    )
    DESCRIPTION: str = Field(
        "Service provider description",
        description="Service provider description (required by CoC)",
    )
    REGISTRATION_POLICY: str = Field(
        "http://example.com/registration-policy/",
        description="Registration policy required by mdpi",
    )
    REGISTRATION_AUTHORITY: str = Field(
        "http://example.com/registration-authority/",
        description="Registration authority required by mdpi",
    )
    REGISTRATION_INSTANT: str = Field(
        datetime.datetime(2017, 1, 1).isoformat(),
        description="Registration instant time required by mdpi",
    )
    ENABLE_SINGLE_LOGOUT: bool = Field(False, description="")
    ALLOW_TO_SELECT_IDENTITY_PROVIDER: bool = Field(True, description="")
    IDENTITY_PROVIDER_URL: str | None = Field(None, description="")
    IDENTITY_PROVIDER_LABEL: str | None = Field(None, description="")
    DEFAULT_BINDING: str = Field(saml2.BINDING_HTTP_POST, description="")
    DISCOVERY_SERVICE_URL: str | None = Field(None, description="")
    DISCOVERY_SERVICE_LABEL: str | None = Field(None, description="")
    MANAGEMENT_URL: str = Field(
        "",
        description="The endpoint for user details management.",
    )

    class Meta:
        public_settings: list[str] = [
            "ENABLE_SINGLE_LOGOUT",
            "ALLOW_TO_SELECT_IDENTITY_PROVIDER",
            "IDENTITY_PROVIDER_URL",
            "IDENTITY_PROVIDER_LABEL",
            "DISCOVERY_SERVICE_URL",
            "DISCOVERY_SERVICE_LABEL",
        ]


class WaldurOpenstack(BaseModel):
    DEFAULT_SECURITY_GROUPS: tuple[
        dict[str, str | tuple[dict[str, str | int], ...]], ...
    ] = Field(
        (
            {
                "name": "ssh",
                "description": "Security group for secure shell access",
                "rules": (
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 22,
                        "to_port": 22,
                    },
                ),
            },
            {
                "name": "ping",
                "description": "Security group for ping",
                "rules": (
                    {
                        "protocol": "icmp",
                        "cidr": "0.0.0.0/0",
                        "icmp_type": -1,
                        "icmp_code": -1,
                    },
                ),
            },
            {
                "name": "rdp",
                "description": "Security group for remote desktop access",
                "rules": (
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 3389,
                        "to_port": 3389,
                    },
                ),
            },
            {
                "name": "web",
                "description": "Security group for http and https access",
                "rules": (
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 80,
                        "to_port": 80,
                    },
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 443,
                        "to_port": 443,
                    },
                ),
            },
        ),
        description="Default security groups and rules created in each of the provisioned OpenStack tenants",
    )

    SUBNET: dict[str, str] = Field(
        {
            "ALLOCATION_POOL_START": "{first_octet}.{second_octet}.{third_octet}.10",
            "ALLOCATION_POOL_END": "{first_octet}.{second_octet}.{third_octet}.200",
        },
        description="Default allocation pool for auto-created internal network",
    )
    DEFAULT_BLACKLISTED_USERNAMES: list[str] = Field(
        ["admin", "service"],
        description="Usernames that cannot be created by Waldur in OpenStack",
    )
    TENANT_CREDENTIALS_VISIBLE: bool = Field(
        False,
        description="If true, generated credentials of a tenant are exposed to project users",
    )
    MAX_CONCURRENT_PROVISION: dict[str, int] = Field(
        {
            "OpenStack.Instance": 4,
            "OpenStack.Volume": 4,
            "OpenStack.Snapshot": 4,
        },
        description="Maximum parallel executions of provisioning operations for OpenStack resources",
    )
    ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS: bool = Field(
        True,
        description="If true, customer users would be offered actions for accessing OpenStack console",
    )
    REQUIRE_AVAILABILITY_ZONE: bool = Field(
        False,
        description="If true, specification of availability zone during provisioning will become mandatory",
    )
    ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION: bool = Field(
        False,
        description="If true, allow connecting of instances directly to external networks",
    )

    class Meta:
        public_settings: list[str] = [
            "ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS",
            "REQUIRE_AVAILABILITY_ZONE",
            "ALLOW_DIRECT_EXTERNAL_NETWORK_CONNECTION",
            "TENANT_CREDENTIALS_VISIBLE",
        ]


class WaldurConfiguration(BaseModel):
    WALDUR_CORE: WaldurCore = WaldurCore()
    WALDUR_AUTH_SOCIAL: WaldurAuthSocial = WaldurAuthSocial()
    WALDUR_HPC: WaldurHPC = WaldurHPC()
    WALDUR_SLURM: WaldurSlurm = WaldurSlurm()
    WALDUR_PID: WaldurPID = WaldurPID()
    WALDUR_OPENPORTAL: WaldurOpenPortal = WaldurOpenPortal()
    WALDUR_OPENSTACK: WaldurOpenstack = WaldurOpenstack()
    WALDUR_AUTH_SAML2: WaldurAuthSAML2 = WaldurAuthSAML2()
    WALDUR_USER_ACTIONS: WaldurUserActions = WaldurUserActions()
    VERIFY_WEBHOOK_REQUESTS: bool = Field(
        True,
        description="When webook is processed, requests verifies SSL certificates for HTTPS requests, just like a web browser.",
    )
    DEFAULT_FROM_EMAIL: str = Field(
        "webmaster@localhost",
        description="Default email address to use for automated correspondence from Waldur.",
    )
    EMAIL_HOOK_FROM_EMAIL: str = Field(
        "",
        description="Alternative email address to use for email hooks.",
    )
    DEFAULT_REPLY_TO_EMAIL: str = Field(
        "",
        description="Default email address to use for email replies.",
    )
    IPSTACK_ACCESS_KEY: str | None = Field(
        None,
        description="Unique authentication key used to gain access to the ipstack API.",
    )
    IMPORT_EXPORT_USE_TRANSACTIONS: bool = Field(
        True,
        description="Controls if resource importing should use database transactions. "
        "Using transactions makes imports safer as a failure during import won't import only part of the data set.",
    )
    LANGUAGES: list[tuple[str, str]] = Field(
        [
            ("en", "English"),
            ("et", "Eesti"),
        ],
        description="The list is a list of two-tuples in the format "
        "(language code, language name) – for example, ('ja', 'Japanese').",
    )
    LANGUAGE_CODE: str = Field(
        "en", description="Represents the name of a default language."
    )

    class Meta:
        public_settings: list[str] = ["LANGUAGES", "LANGUAGE_CODE"]
