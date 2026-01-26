LANGUAGE_CHOICES = [
    "en",
    "et",
    "lt",
    "lv",
    "ru",
    "it",
    "de",
    "da",
    "sv",
    "es",
    "fr",
    "nb",
    "ar",
    "cs",
]

CONSTANCE_BACKEND = "constance.backends.database.DatabaseBackend"
CONSTANCE_DBS = "default"
CONSTANCE_SUPERUSER_ONLY = False
CONSTANCE_IGNORE_ADMIN_VERSION_CHECK = True
CONSTANCE_ADDITIONAL_FIELDS = {
    "image_field": ["django.forms.ImageField", {"required": False}],
    "email_field": ["django.forms.EmailField", {"required": False}],
    "color_field": ["django.forms.CharField", {"required": False}],
    "html_field": ["django.forms.CharField", {"required": False}],
    "text_field": ["django.forms.CharField", {"required": False}],
    "url_field": ["django.forms.URLField", {"required": False}],
    "secret_field": ["django.forms.CharField", {"required": False}],
    "dict_field": ["waldur_core.core.serializers.DictField", {"required": False}],
    "list_field": ["waldur_core.core.serializers.ListField", {"required": False}],
    "json_list_field": [
        "waldur_core.core.serializers.JsonListField",
        {"required": False},
    ],
    "country_list_field": [
        "waldur_core.core.serializers.ListField",
        {"required": False},
    ],
    "choice_field": ["django.forms.ChoiceField", {"required": False}],
    "multilingual_image_field": [
        "waldur_core.core.forms.MultilingualImageField",
        {"required": False},
    ],
}
CONSTANCE_CONFIG = {
    "SITE_NAME": ("Waldur", "Human-friendly name of the Waldur deployment."),
    "SITE_DESCRIPTION": (
        "Your single pane of control for managing projects, teams and resources in a self-service manner.",
        "Description of the Waldur deployment.",
    ),
    "HOMEPORT_URL": (
        "https://example.com/",
        "It is used for rendering callback URL in HomePort",
    ),
    "RANCHER_USERNAME_INPUT_LABEL": (
        "Username",
        "Label for the username field in Rancher external user resource access management.",
    ),
    "SITE_ADDRESS": ("", "It is used in marketplace order header."),
    "SITE_EMAIL": ("", "It is used in marketplace order header and UI footer."),
    "SITE_PHONE": ("", "It is used in marketplace order header and UI footer."),
    "CURRENCY_NAME": (
        "EUR",
        "It is used in marketplace order details and invoices for currency formatting.",
    ),
    "THUMBNAIL_SIZE": (
        "120x120",
        "Size of the thumbnail to generate when screenshot is uploaded for an offering.",
    ),
    "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": (
        True,
        "Allow anonymous users to see shared offerings in active, paused and archived states",
    ),
    "ANONYMOUS_USER_CAN_VIEW_PLANS": (True, "Allow anonymous users to see plans"),
    "NOTIFY_STAFF_ABOUT_APPROVALS": (
        False,
        "If true, users with staff role are notified when request for order approval is generated",
    ),
    "NOTIFY_ABOUT_RESOURCE_CHANGE": (
        True,
        "If true, notify users about resource changes from Marketplace perspective. Can generate duplicate events if plugins also log",
    ),
    "DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE": (
        True,
        "Disable only resource update events.",
    ),
    "MARKETPLACE_LANDING_PAGE": (
        "Marketplace",
        "Marketplace landing page title.",
    ),
    "ENABLE_STALE_RESOURCE_NOTIFICATIONS": (
        False,
        "Enable reminders to owners about resources of shared offerings that have not generated any cost for the last 3 months.",
    ),
    "TELEMETRY_URL": (
        "https://telemetry.waldur.com/",
        "URL for sending telemetry data.",
    ),
    "TELEMETRY_VERSION": (1, "Telemetry service version."),
    "SCRIPT_RUN_MODE": (
        "docker",
        'Type of jobs deployment. Valid values: "docker" for simple docker deployment, "k8s" for Kubernetes-based one',
    ),
    "DOCKER_CLIENT": (
        {"base_url": "unix:///var/run/docker.sock"},
        "Options for docker client. See also: <https://docker-py.readthedocs.io/en/stable/client.html#docker.client.DockerClient>",
        "dict_field",
    ),
    "DOCKER_RUN_OPTIONS": (
        {"mem_limit": "512m"},
        "Options for docker runtime. See also: <https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.ContainerCollection.run>",
        "dict_field",
    ),
    "DOCKER_SCRIPT_DIR": (
        "",
        "Path to folder on executor machine where to create temporary submission scripts. If None, uses OS-dependent location. OS X users, see <https://github.com/docker/for-mac/issues/1532>",
    ),
    "DOCKER_REMOVE_CONTAINER": (True, "Remove Docker container after script execution"),
    "DOCKER_IMAGES": (
        {
            "python": {"image": "python:3.11-alpine", "command": "python"},
            "shell": {"image": "alpine:3", "command": "sh"},
            "ansible": {
                "image": "alpine/ansible:2.18.6",
                "command": "ansible-playbook",
            },
        },
        "Key is command to execute script, value is a dictionary of image name and command.",
        "dict_field",
    ),
    "DOCKER_VOLUME_NAME": (
        "waldur-docker-compose_waldur_script_launchzone",
        "A name of the shared volume to store scripts",
    ),
    "K8S_NAMESPACE": ("default", "Kubernetes namespace where jobs will be executed"),
    "K8S_CONFIG_PATH": ("~/.kube/config", "Path to Kubernetes configuration file"),
    "K8S_JOB_TIMEOUT": (
        30 * 60,
        "Timeout for execution of one Kubernetes job in seconds",
    ),
    "ENABLE_STRICT_CHECK_ACCEPTING_INVITATION": (
        False,
        "If true, user email in Waldur database and in invitatation must strictly match.",
    ),
    "INVITATION_DISABLE_MULTIPLE_ROLES": (
        False,
        "Do not allow user to accept multiple roles within the same scope (project or organization) using invitation. When enabled, users can still accept invitations to different scopes but cannot have multiple roles in the same scope.",
    ),
    "DEFAULT_IDP": ("", "Triggers authentication flow at once."),
    "DOCS_URL": ("", "Renders link to docs in header", "url_field"),
    "SHORT_PAGE_TITLE": ("Waldur", "It is used as prefix for page title."),
    "FULL_PAGE_TITLE": (
        "Waldur | Cloud Service Management",
        "It is used as default page title if it's not specified explicitly.",
    ),
    "PROJECT_END_DATE_MANDATORY": (
        False,
        "If true, project end date field becomes mandatory when creating or updating projects.",
    ),
    "ENABLE_ORDER_START_DATE": (
        False,
        "Allow setting start date to control when resource creation order is processed.",
    ),
    "BRAND_COLOR": (
        "#307300",
        "Brand color is used for button background.",
        "color_field",
    ),
    "HERO_LINK_LABEL": (
        "",
        "Label for link in hero section of HomePort landing page. It can be lead to support site or blog post.",
    ),
    "HERO_LINK_URL": (
        "",
        "Link URL in hero section of HomePort landing page.",
        "url_field",
    ),
    "SUPPORT_PORTAL_URL": (
        "",
        "Link URL to support portal. Rendered as a shortcut on dashboard",
        "url_field",
    ),
    "COMMON_FOOTER_TEXT": (
        "",
        "Common footer in txt format for all emails.",
        "text_field",
    ),
    "COMMON_FOOTER_HTML": (
        "",
        "Common footer in html format for all emails.",
        "html_field",
    ),
    "LANGUAGE_CHOICES": (
        ",".join(LANGUAGE_CHOICES),
        "List of enabled languages",
    ),
    "DISABLE_DARK_THEME": (False, "Toggler to disable dark theme."),
    # images, logos, favicons
    "POWERED_BY_LOGO": (
        "",
        "The image rendered at the bottom of login menu in HomePort.",
        "image_field",
    ),
    "HERO_IMAGE": (
        "",
        "The image rendered at hero section of HomePort landing page.",
        "image_field",
    ),
    "MARKETPLACE_HERO_IMAGE": (
        "",
        "The image rendered at hero section of Marketplace landing page. Please, use a wide image (min. 1920×600px) with no text or logos. Keep the center area clean, and choose a darker image for dark mode or a brighter image for light mode.",
        "image_field",
    ),
    "CALL_MANAGEMENT_HERO_IMAGE": (
        "",
        "The image rendered at hero section of Call Management landing page. Please, use a wide image (min. 1920×600px) with no text or logos. Keep the center area clean, and choose a darker image for dark mode or a brighter image for light mode.",
        "image_field",
    ),
    "SIDEBAR_LOGO": (
        "",
        "The image rendered at the top of sidebar menu in HomePort.",
        "image_field",
    ),
    "SIDEBAR_LOGO_DARK": (
        "",
        "The image rendered at the top of sidebar menu in dark mode.",
        "image_field",
    ),
    "SIDEBAR_LOGO_MOBILE": (
        "",
        "The image rendered at the top of mobile sidebar menu in HomePort.",
        "image_field",
    ),
    "SIDEBAR_STYLE": (
        "dark",
        "Style of sidebar. Possible values: dark, light, accent.",
    ),
    "SITE_LOGO": ("", "The image used in marketplace order header.", "image_field"),
    "LOGIN_LOGO": ("", "A custom .png image file for login page", "image_field"),
    "LOGIN_LOGO_MULTILINGUAL": (
        {},
        "Language-specific login logos. Dict mapping language codes to image paths, "
        "e.g., {'de': 'path/to/german_logo.png'}. "
        "Falls back to LOGIN_LOGO if requested language not found.",
        "multilingual_image_field",
    ),
    "LOGIN_PAGE_LAYOUT": (
        "split-screen",
        "Login page layout style. Options: split-screen, centered-card, minimal, full-hero, "
        "gradient, stacked, right-split, glassmorphism, neumorphism, animated-gradient, "
        "video-background, bottom-sheet, tabbed, wizard, stats, news, carousel, "
        "logo-watermark, brand-pattern, duotone, diagonal, time-based, seasonal, weather.",
    ),
    "LOGIN_PAGE_VIDEO_URL": (
        "",
        "Video URL for the video-background login page layout. "
        "Supports MP4 format. Leave empty to use default sample video.",
        "url_field",
    ),
    "LOGIN_PAGE_STATS": (
        [],
        "Stats displayed in the Stats login page layout. "
        "List of objects with 'value' and 'label' keys, "
        "e.g., [{'value': '10K+', 'label': 'Active Users'}, {'value': '99.9%', 'label': 'Uptime'}].",
        "json_list_field",
    ),
    "LOGIN_PAGE_CAROUSEL_SLIDES": (
        [],
        "Carousel slides displayed in the Carousel login page layout. "
        "List of objects with 'title' and 'subtitle' keys, "
        "e.g., [{'title': 'Welcome', 'subtitle': 'Get started with our platform'}].",
        "json_list_field",
    ),
    "LOGIN_PAGE_NEWS": (
        [],
        "News items displayed in the News login page layout. "
        "List of objects with 'date', 'title', 'description', and 'tag' keys. "
        "Supported tags: Feature, Update, Security, Announcement, Maintenance. "
        "Example: [{'date': 'Jan 2025', 'title': 'New Feature', 'description': 'Description here', 'tag': 'Feature'}].",
        "json_list_field",
    ),
    "FAVICON": ("", "A custom favicon .png image file", "image_field"),
    "OFFERING_LOGO_PLACEHOLDER": ("", "Default logo for offering", "image_field"),
    # service desk integration settings
    "WALDUR_SUPPORT_ENABLED": (
        True,
        "Toggler for support plugin.",
    ),
    "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": (
        "atlassian",
        "Type of support backend. Possible values: atlassian, zammad, smax.",
    ),
    "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE": (
        True,
        "Toggler for request type displaying",
    ),
    # Atlassian settings
    "ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS": (
        False,
        "Toggler for mapping between waldur user and service desk agents.",
    ),
    "ATLASSIAN_API_URL": (
        "https://example.com/",
        "Atlassian API server URL",
        "url_field",
    ),
    "ATLASSIAN_USERNAME": ("USERNAME", "Username for access user"),
    "ATLASSIAN_PASSWORD": ("PASSWORD", "Password for access user", "secret_field"),
    "ATLASSIAN_EMAIL": ("", "Email for access user", "email_field"),
    "ATLASSIAN_USE_OLD_API": (
        False,
        "Toggler for legacy API usage.",
    ),
    "ATLASSIAN_TOKEN": ("", "Token for access user", "secret_field"),
    "ATLASSIAN_PERSONAL_ACCESS_TOKEN": (
        "",
        "Personal Access Token for user",
        "secret_field",
    ),
    "ATLASSIAN_OAUTH2_CLIENT_ID": ("", "OAuth 2.0 Client ID", "secret_field"),
    "ATLASSIAN_OAUTH2_ACCESS_TOKEN": ("", "OAuth 2.0 Access Token", "secret_field"),
    "ATLASSIAN_OAUTH2_TOKEN_TYPE": ("Bearer", "OAuth 2.0 Token Type"),
    "ATLASSIAN_VERIFY_SSL": (
        True,
        "Toggler for SSL verification",
    ),
    "ATLASSIAN_PROJECT_ID": ("", "Service desk ID or key"),
    "ATLASSIAN_SHARED_USERNAME": (
        False,
        "Is Service Desk username the same as in Waldur",
    ),
    "ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED": (
        True,
        "Should extra issue field mappings be applied",
    ),
    "ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE": (
        "Service Request",
        "Issue type used for request-based item processing.",
    ),
    "ATLASSIAN_EXCLUDED_ATTACHMENT_TYPES": (
        "",
        "Comma-separated list of file extenstions not allowed for attachment.",
    ),
    "ATLASSIAN_DESCRIPTION_TEMPLATE": ("", "Template for issue description"),
    "ATLASSIAN_SUMMARY_TEMPLATE": ("", "Template for issue summary"),
    "ATLASSIAN_AFFECTED_RESOURCE_FIELD": ("", "Affected resource field name"),
    "ATLASSIAN_IMPACT_FIELD": ("Impact", "Impact field name"),
    "ATLASSIAN_ORGANISATION_FIELD": ("", "Organisation field name"),
    "ATLASSIAN_RESOLUTION_SLA_FIELD": ("", "Resolution SLA field name"),
    "ATLASSIAN_PROJECT_FIELD": ("", "Project field name"),
    "ATLASSIAN_REPORTER_FIELD": ("Original Reporter", "Reporter field name"),
    "ATLASSIAN_CALLER_FIELD": ("Caller", "Caller field name"),
    "ATLASSIAN_SLA_FIELD": ("Time to first response", "SLA field name"),
    "ATLASSIAN_LINKED_ISSUE_TYPE": ("Relates", "Type of linked issue field name"),
    "ATLASSIAN_SATISFACTION_FIELD": (
        "Customer satisfaction",
        "Customer satisfaction field name",
    ),
    "ATLASSIAN_REQUEST_FEEDBACK_FIELD": (
        "Request feedback",
        "Request feedback field name",
    ),
    "ATLASSIAN_TEMPLATE_FIELD": ("", "Template field name"),
    "ATLASSIAN_WALDUR_BACKEND_ID_FIELD": (
        "customfield_10200",
        "Waldur backend ID custom field ID (fallback when field lookup by name fails)",
    ),
    # Zammad settings
    "ZAMMAD_API_URL": (
        "",
        "Zammad API server URL. For example <https://localhost:8080/>",
        "url_field",
    ),
    "ZAMMAD_TOKEN": ("", "Authorization token.", "secret_field"),
    "ZAMMAD_GROUP": (
        "",
        "The name of the group to which the ticket will be added. "
        "If not specified, the first group will be used.",
    ),
    "ZAMMAD_ARTICLE_TYPE": (
        "email",
        "Type of a comment. "
        "Default is email because it allows support to reply to tickets directly in Zammad"
        "<https://docs.zammad.org/en/latest/api/ticket/articles.html#articles/>",
    ),
    "ZAMMAD_COMMENT_MARKER": (
        "Created by Waldur",
        "Marker for comment. "
        "Used for separating comments made via Waldur from natively added "
        "comments.",
    ),
    "ZAMMAD_COMMENT_PREFIX": ("User: {name}", "Comment prefix with user info."),
    "ZAMMAD_COMMENT_COOLDOWN_DURATION": (
        5,
        "Time in minutes. "
        "Time in minutes while comment deletion is available "
        "<https://github.com/zammad/zammad/issues/2687/>, "
        "<https://github.com/zammad/zammad/issues/3086/>",
    ),
    # SMAX settings
    "SMAX_API_URL": (
        "",
        "SMAX API server URL. For example <https://localhost:8080/>",
        "url_field",
    ),
    "SMAX_TENANT_ID": ("", "User tenant ID."),
    "SMAX_LOGIN": ("", "Authorization login."),
    "SMAX_PASSWORD": ("", "Authorization password.", "secret_field"),
    "SMAX_ORGANISATION_FIELD": ("", "Organisation field name."),
    "SMAX_PROJECT_FIELD": ("", "Project field name."),
    "SMAX_AFFECTED_RESOURCE_FIELD": ("", "Resource field name."),
    "SMAX_TIMES_TO_PULL": (
        10,
        "The maximum number of attempts to pull user from backend.",
    ),
    "SMAX_SECONDS_TO_WAIT": (
        1,
        "Duration in seconds of delay between pull user attempts.",
    ),
    "SMAX_CREATION_SOURCE_NAME": ("", "Creation source name."),
    "SMAX_REQUESTS_OFFERING": ("", "Requests offering code for all issues."),
    "SMAX_VERIFY_SSL": (True, "Toggler for SSL verification"),
    # Service accounts
    "ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND": (
        False,
        "Enable mock returns for the service account service",
    ),
    # Course accounts
    "ENABLE_MOCK_COURSE_ACCOUNT_BACKEND": (
        False,
        "Enable mock returns for the course account service",
    ),
    # Proposal settings
    "PROPOSAL_REVIEW_DURATION": (7, "Review duration in days."),
    # ORCID integration settings
    "ORCID_CLIENT_ID": (
        "",
        "ORCID OAuth2 Client ID for reviewer profile integration.",
    ),
    "ORCID_CLIENT_SECRET": (
        "",
        "ORCID OAuth2 Client Secret.",
        "secret_field",
    ),
    "ORCID_REDIRECT_URI": (
        "",
        "ORCID OAuth2 Redirect URI. Typically {HOMEPORT_URL}/orcid-callback/",
        "url_field",
    ),
    "ORCID_API_URL": (
        "https://pub.orcid.org/v3.0",
        "ORCID API Base URL. Use https://pub.sandbox.orcid.org/v3.0 for testing.",
        "url_field",
    ),
    "ORCID_AUTH_URL": (
        "https://orcid.org/oauth",
        "ORCID OAuth Authorization URL. Use https://sandbox.orcid.org/oauth for testing.",
        "url_field",
    ),
    "ORCID_SANDBOX_MODE": (
        False,
        "Use ORCID sandbox environment for testing. When enabled, uses sandbox URLs automatically.",
    ),
    # External publication API settings
    "SEMANTIC_SCHOLAR_API_KEY": (
        "",
        "Semantic Scholar API Key for publication imports. Optional but recommended for higher rate limits.",
        "secret_field",
    ),
    "CROSSREF_MAILTO": (
        "",
        "Email address for CrossRef API polite pool. Provides higher rate limits.",
        "email_field",
    ),
    # Reviewer profile settings
    "REVIEWER_PROFILES_ENABLED": (
        True,
        "Enable reviewer profile management features.",
    ),
    "COI_DETECTION_ENABLED": (
        True,
        "Enable conflict of interest detection features.",
    ),
    "COI_DISCLOSURE_REQUIRED": (
        False,
        "Require reviewers to submit COI disclosure before reviewing proposals.",
    ),
    "AUTOMATED_MATCHING_ENABLED": (
        True,
        "Enable automated reviewer-proposal matching algorithms.",
    ),
    "COI_COAUTHORSHIP_LOOKBACK_YEARS": (
        5,
        "Default number of years to look back for co-authorship COI detection.",
    ),
    "COI_COAUTHORSHIP_THRESHOLD_PAPERS": (
        2,
        "Default number of co-authored papers to trigger a COI.",
    ),
    "COI_INSTITUTIONAL_LOOKBACK_YEARS": (
        3,
        "Default number of years after leaving institution before COI expires.",
    ),
    "USER_TABLE_COLUMNS": ("", "Comma-separated list of columns for users table."),
    "AUTO_APPROVE_USER_TOS": (
        False,
        "Mark terms of services as approved for new users.",
    ),
    # FREEIPA settings
    "FREEIPA_ENABLED": (
        False,
        "Enable integration of identity provisioning in configured FreeIPA.",
    ),
    "FREEIPA_HOSTNAME": ("ipa.example.com", "Hostname of FreeIPA server."),
    "FREEIPA_USERNAME": (
        "admin",
        "Username of FreeIPA user with administrative privileges.",
    ),
    "FREEIPA_PASSWORD": (
        "secret",
        "Password of FreeIPA user with administrative privileges",
        "secret_field",
    ),
    "FREEIPA_VERIFY_SSL": (
        True,
        "Validate TLS certificate of FreeIPA web interface / REST API",
    ),
    "FREEIPA_USERNAME_PREFIX": (
        "waldur_",
        "Prefix to be appended to all usernames created in FreeIPA by Waldur",
    ),
    "FREEIPA_GROUPNAME_PREFIX": (
        "waldur_",
        "Prefix to be appended to all group names created in FreeIPA by Waldur",
    ),
    "FREEIPA_BLACKLISTED_USERNAMES": (
        ["root"],
        "List of username that users are not allowed to select",
        "list_field",
    ),
    "FREEIPA_GROUP_SYNCHRONIZATION_ENABLED": (
        True,
        "Optionally disable creation of user groups in FreeIPA matching Waldur structure",
    ),
    "KEYCLOAK_ICON": (
        "",
        "A custom PNG icon for Keycloak login button",
        "image_field",
    ),
    "COUNTRIES": (
        [
            "AL",
            "AT",
            "BA",
            "BE",
            "BG",
            "CH",
            "CY",
            "CZ",
            "DE",
            "DK",
            "EE",
            "ES",
            "EU",
            "FI",
            "FR",
            "GB",
            "GE",
            "GR",
            "HR",
            "HU",
            "IE",
            "IS",
            "IT",
            "LT",
            "LU",
            "LV",
            "MC",
            "MK",
            "MT",
            "NL",
            "NO",
            "PL",
            "PT",
            "RO",
            "RS",
            "SE",
            "SI",
            "SK",
            "UA",
        ],
        "It is used in organization creation dialog in order to limit country choices to predefined set.",
        "country_list_field",
    ),
    "OIDC_AUTH_URL": (
        "",
        "OIDC authorization endpoint URL. Reserved for future OAuth 2.0 authorization code flow integration.",
    ),
    "OIDC_INTROSPECTION_URL": (
        "",
        "RFC 7662 Token Introspection endpoint URL. Used to validate API bearer tokens. "
        "When a client sends Authorization: Bearer <token>, Waldur calls this endpoint to verify the token is active.",
    ),
    "OIDC_CLIENT_ID": (
        "",
        "Client ID for HTTP Basic authentication when calling the token introspection endpoint. "
        "Required together with OIDC_CLIENT_SECRET and OIDC_INTROSPECTION_URL.",
    ),
    "OIDC_CLIENT_SECRET": (
        "",
        "Client secret for HTTP Basic authentication when calling the token introspection endpoint. "
        "Required together with OIDC_CLIENT_ID and OIDC_INTROSPECTION_URL.",
    ),
    "OIDC_USER_FIELD": (
        "username",
        "Field name from the introspection response JSON used to identify the Waldur user. "
        "Common values: 'username', 'email', 'sub', 'client_id'. The value is matched against User.username.",
    ),
    "OIDC_CACHE_TIMEOUT": (
        300,
        "Seconds to cache successful token introspection results. Reduces load on the introspection endpoint. "
        "Set to 0 to disable caching. Default: 300 (5 minutes).",
    ),
    "OIDC_ACCESS_TOKEN_ENABLED": (
        False,
        "If true, OIDC complete view returns access token instead of Waldur token",
    ),
    "OIDC_BLOCK_CREATION_OF_UNINVITED_USERS": (
        False,
        "If true, block creation of an account on OIDC login if user email is not provided or provided and is not in the list of one of the active invitations.",
    ),
    "DEACTIVATE_USER_IF_NO_ROLES": (
        False,
        "Deactivate user if all roles are revoked (except staff/support)",
    ),
    "WALDUR_AUTH_SOCIAL_ROLE_CLAIM": (
        "",
        "OAuth/OIDC token claim name containing user roles for automatic staff/support assignment. "
        "If the claim contains 'staff', user gets is_staff=True. If it contains 'support', user gets is_support=True. "
        "Leave empty to disable role synchronization from identity provider.",
    ),
    "DEFAULT_OFFERING_USER_ATTRIBUTES": (
        ["username", "full_name", "email"],
        "Default user attributes exposed to service providers (OfferingUser API) when no explicit config exists. "
        "Available options: username, full_name, email, phone_number, organization, job_title, affiliations, "
        "gender, personal_title, birth_date, place_of_birth, "
        "country_of_residence, nationality, nationalities, "
        "organization_country, organization_type, eduperson_assurance, "
        "civil_number, identity_source.",
        "list_field",
    ),
    "INVITATION_ALLOWED_FIELDS": (
        ["full_name", "organization", "job_title"],
        "Fields that can be provided in invitations for email personalization. These are NOT copied to user profile.",
        "list_field",
    ),
    "ENABLED_USER_PROFILE_ATTRIBUTES": (
        ["phone_number", "organization", "job_title", "affiliations"],
        "List of enabled user profile attributes. Controls IdP sync and UI display. "
        "Core attributes (username, email, first_name, last_name, full_name) are always enabled. "
        "Available options: phone_number, organization, job_title, affiliations, "
        "gender, personal_title, birth_date, place_of_birth, "
        "country_of_residence, nationality, nationalities, "
        "organization_country, organization_type, eduperson_assurance, "
        "civil_number, identity_source.",
        "list_field",
    ),
    "MANDATORY_USER_ATTRIBUTES": (
        [],
        "List of user profile attributes that are mandatory. Users with missing mandatory "
        "attributes will have limited API access until their profile is complete. "
        "Available: phone_number, organization, job_title, affiliations, civil_number, "
        "first_name, last_name, email, etc.",
        "list_field",
    ),
    "ENFORCE_MANDATORY_USER_ATTRIBUTES": (
        False,
        "If True, users with incomplete mandatory attributes will be blocked from most API "
        "endpoints until they complete their profile.",
    ),
    "MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES": (
        60,
        "How many minutes before scheduled maintenance users should be notified.",
    ),
    "MAINTENANCE_ANNOUNCEMENT_NOTIFY_SYSTEM": (
        ["AdminAnnouncement"],
        "How maintenance notifications are delivered. Choices: AdminAnnouncement or BroadcastMessage.",
        "list_field",
    ),
    "ENFORCE_USER_CONSENT_FOR_OFFERINGS": (
        False,
        "If True, users must have active consent to access offerings that have active Terms of Service.",
    ),
    "DISABLED_OFFERING_TYPES": (
        [],
        "List of offering types disabled for creation and selection.",
        "list_field",
    ),
    "ONBOARDING_VALIDATION_METHODS": (
        [],
        "List of automatic validation methods available for this portal (e.g., ariregister, wirtschaftscompass, bolagsverket). Must match backend method names.",
        "list_field",
    ),
    "ONBOARDING_VERIFICATION_EXPIRY_HOURS": (
        48,
        "Number of hours after which onboarding verifications expire.",
    ),
    "ONBOARDING_ARIREGISTER_BASE_URL": (
        "https://demo-ariregxmlv6.rik.ee/",
        "Base URL for Estonian Äriregister API endpoint.",
        "url_field",
    ),
    "ONBOARDING_ARIREGISTER_USERNAME": (
        "",
        "Username for Estonian Äriregister API authentication.",
    ),
    "ONBOARDING_ARIREGISTER_PASSWORD": (
        "",
        "Password for Estonian Äriregister API authentication.",
        "secret_field",
    ),
    "ONBOARDING_ARIREGISTER_TIMEOUT": (
        30,
        "Timeout in seconds for Estonian Äriregister API requests.",
    ),
    "ONBOARDING_WICO_API_URL": (
        "https://api.wirtschaftscompass.at/",
        "WirtschaftsCompass API server URL",
        "url_field",
    ),
    "ONBOARDING_WICO_TOKEN": ("", "WirtschaftsCompass API token", "secret_field"),
    "ONBOARDING_BOLAGSVERKET_API_URL": (
        "https://gw-accept2.api.bolagsverket.se/",
        "Sweden Business Register API server URL",
        "url_field",
    ),
    "ONBOARDING_BOLAGSVERKET_TOKEN_API_URL": (
        "https://portal-accept2.api.bolagsverket.se/",
        "Bolagsverket OAuth2 token server base URL",
        "url_field",
    ),
    "ONBOARDING_BOLAGSVERKET_CLIENT_ID": (
        "",
        "Sweden Business Register API client identifier",
    ),
    "ONBOARDING_BOLAGSVERKET_CLIENT_SECRET": (
        "",
        "Sweden Business Register API client secret",
        "secret_field",
    ),
    "ONBOARDING_BREG_API_URL": (
        "https://data.brreg.no/",
        "Norway Business Register API server URL",
        "url_field",
    ),
    # LLM inference settings
    "LLM_CHAT_ENABLED": (
        False,
        "Enable LLM-based chat feature and calls to the inference service.",
    ),
    "LLM_INFERENCES_BACKEND_TYPE": (
        "ollama",
        "Type of LLM inference backend. For example: openai, ollama.",
    ),
    "LLM_INFERENCES_API_URL": (
        "",
        "Base URL for LLM inference service API.",
        "url_field",
    ),
    "LLM_INFERENCES_API_TOKEN": (
        "",
        "API key for authenticating with the LLM inference service.",
        "secret_field",
    ),
    "LLM_INFERENCES_MODEL": (
        "gemma3:27b",
        "Name of the LLM model to use for inference.",
    ),
    # Software catalog settings
    "SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED": (
        True,
        "Enable automated daily updates for EESSI software catalog",
    ),
    "SOFTWARE_CATALOG_EESSI_VERSION": (
        "",
        "EESSI catalog version to load (auto-detect if empty)",
    ),
    "SOFTWARE_CATALOG_EESSI_API_URL": (
        "https://www.eessi.io/api_data/data/",
        "Base URL for EESSI API data",
    ),
    "SOFTWARE_CATALOG_EESSI_INCLUDE_EXTENSIONS": (
        True,
        "Include extension packages (Python, R packages, etc.) from EESSI",
    ),
    "SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED": (
        True,
        "Enable automated daily updates for Spack software catalog",
    ),
    "SOFTWARE_CATALOG_SPACK_VERSION": (
        "",
        "Spack catalog version to load (auto-detect if empty)",
    ),
    "SOFTWARE_CATALOG_SPACK_DATA_URL": (
        "https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json",
        "URL for Spack repology.json data",
    ),
    "SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES": (
        True,
        "Update existing packages during catalog refresh",
    ),
    "SOFTWARE_CATALOG_CLEANUP_ENABLED": (
        True,
        "Enable automatic cleanup of old catalog data",
    ),
    "SOFTWARE_CATALOG_RETENTION_DAYS": (
        90,
        "Number of days to retain old catalog versions",
    ),
    # Table Growth Monitoring settings
    "TABLE_GROWTH_MONITORING_ENABLED": (
        True,
        "Enable table growth monitoring to detect potential data leaks from bugs.",
    ),
    "TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT": (
        50,
        "Alert if a table grows by more than this percentage in a week.",
    ),
    "TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT": (
        200,
        "Alert if a table grows by more than this percentage in a month.",
    ),
    "TABLE_GROWTH_RETENTION_DAYS": (
        90,
        "Number of days to retain table size history data.",
    ),
    "TABLE_GROWTH_MIN_SIZE_BYTES": (
        1048576,
        "Minimum table size in bytes (default 1MB) to monitor. Smaller tables are ignored.",
    ),
    # User Actions Configuration
    "USER_ACTIONS_ENABLED": (
        True,
        "Enable user actions notification system.",
    ),
    "USER_ACTIONS_PENDING_ORDER_HOURS": (
        24,
        "Hours before pending order becomes a user action item (1-168).",
    ),
    "USER_ACTIONS_HIGH_URGENCY_NOTIFICATION": (
        True,
        "Send digest notification if user has high urgency actions.",
    ),
    "USER_ACTIONS_NOTIFICATION_THRESHOLD": (
        5,
        "Send digest notification if user has more than N actions.",
    ),
    "USER_ACTIONS_EXECUTION_RETENTION_DAYS": (
        90,
        "Number of days to keep action execution history.",
    ),
    "USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS": (
        [30, 14, 7, 1],
        "Default reminder schedule (days before expiration) for expiring resources. Can be overridden per offering via plugin_options.resource_expiration_reminders.",
        "list_field",
    ),
    # User Data Access Logging settings
    "USER_DATA_ACCESS_LOGGING_ENABLED": (
        False,
        "Enable logging of user profile data access events for GDPR compliance.",
    ),
    "USER_DATA_ACCESS_LOG_RETENTION_DAYS": (
        90,
        "Number of days to retain user data access logs before automatic cleanup.",
    ),
    "USER_DATA_ACCESS_LOG_SELF_ACCESS": (
        False,
        "Log when users access their own profile data. Disabled by default to reduce log volume.",
    ),
}

CONSTANCE_CONFIG_FIELDSETS = {
    "Branding": (
        "SITE_NAME",
        "SHORT_PAGE_TITLE",
        "FULL_PAGE_TITLE",
        "SITE_DESCRIPTION",
        "HOMEPORT_URL",
        "RANCHER_USERNAME_INPUT_LABEL",
    ),
    "Marketplace Branding": (
        "SITE_ADDRESS",
        "SITE_EMAIL",
        "SITE_PHONE",
        "CURRENCY_NAME",
        "MARKETPLACE_LANDING_PAGE",
        "COUNTRIES",
    ),
    "Marketplace": (
        "THUMBNAIL_SIZE",
        "ANONYMOUS_USER_CAN_VIEW_OFFERINGS",
        "ANONYMOUS_USER_CAN_VIEW_PLANS",
        "NOTIFY_STAFF_ABOUT_APPROVALS",
        "NOTIFY_ABOUT_RESOURCE_CHANGE",
        "DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE",
        "ENABLE_STALE_RESOURCE_NOTIFICATIONS",
        "ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND",
        "ENABLE_MOCK_COURSE_ACCOUNT_BACKEND",
        "ENFORCE_USER_CONSENT_FOR_OFFERINGS",
        "DISABLED_OFFERING_TYPES",
        "ENABLE_ORDER_START_DATE",
    ),
    "Project": ("PROJECT_END_DATE_MANDATORY",),
    "Telemetry": (
        "TELEMETRY_URL",
        "TELEMETRY_VERSION",
    ),
    "Custom Scripts": (
        "SCRIPT_RUN_MODE",
        "DOCKER_CLIENT",
        "DOCKER_RUN_OPTIONS",
        "DOCKER_SCRIPT_DIR",
        "DOCKER_REMOVE_CONTAINER",
        "DOCKER_IMAGES",
        "DOCKER_VOLUME_NAME",
        "K8S_NAMESPACE",
        "K8S_CONFIG_PATH",
        "K8S_JOB_TIMEOUT",
    ),
    "Notifications": (
        "COMMON_FOOTER_TEXT",
        "COMMON_FOOTER_HTML",
        "MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES",
        "MAINTENANCE_ANNOUNCEMENT_NOTIFY_SYSTEM",
    ),
    "Links": (
        "DOCS_URL",
        "HERO_LINK_LABEL",
        "HERO_LINK_URL",
        "SUPPORT_PORTAL_URL",
    ),
    "Theme": (
        "SIDEBAR_STYLE",
        "BRAND_COLOR",
        "DISABLE_DARK_THEME",
    ),
    "Login page": (
        "LOGIN_PAGE_LAYOUT",
        "LOGIN_PAGE_VIDEO_URL",
        "LOGIN_PAGE_STATS",
        "LOGIN_PAGE_CAROUSEL_SLIDES",
        "LOGIN_PAGE_NEWS",
    ),
    "Images": (
        "SITE_LOGO",
        "SIDEBAR_LOGO",
        "SIDEBAR_LOGO_MOBILE",
        "SIDEBAR_LOGO_DARK",
        "POWERED_BY_LOGO",
        "HERO_IMAGE",
        "MARKETPLACE_HERO_IMAGE",
        "CALL_MANAGEMENT_HERO_IMAGE",
        "LOGIN_LOGO",
        "LOGIN_LOGO_MULTILINGUAL",
        "FAVICON",
        "OFFERING_LOGO_PLACEHOLDER",
        "KEYCLOAK_ICON",
    ),
    "Service desk integration settings": (
        "WALDUR_SUPPORT_ENABLED",
        "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE",
        "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE",
    ),
    "Atlassian settings": (
        "ATLASSIAN_API_URL",
        "ATLASSIAN_USERNAME",
        "ATLASSIAN_PASSWORD",
        "ATLASSIAN_EMAIL",
        "ATLASSIAN_TOKEN",
        "ATLASSIAN_PERSONAL_ACCESS_TOKEN",
        "ATLASSIAN_OAUTH2_CLIENT_ID",
        "ATLASSIAN_OAUTH2_ACCESS_TOKEN",
        "ATLASSIAN_OAUTH2_TOKEN_TYPE",
        "ATLASSIAN_PROJECT_ID",
        "ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE",
        "ATLASSIAN_EXCLUDED_ATTACHMENT_TYPES",
        "ATLASSIAN_AFFECTED_RESOURCE_FIELD",
        "ATLASSIAN_DESCRIPTION_TEMPLATE",
        "ATLASSIAN_SUMMARY_TEMPLATE",
        "ATLASSIAN_IMPACT_FIELD",
        "ATLASSIAN_ORGANISATION_FIELD",
        "ATLASSIAN_RESOLUTION_SLA_FIELD",
        "ATLASSIAN_PROJECT_FIELD",
        "ATLASSIAN_REPORTER_FIELD",
        "ATLASSIAN_CALLER_FIELD",
        "ATLASSIAN_SLA_FIELD",
        "ATLASSIAN_LINKED_ISSUE_TYPE",
        "ATLASSIAN_SATISFACTION_FIELD",
        "ATLASSIAN_REQUEST_FEEDBACK_FIELD",
        "ATLASSIAN_TEMPLATE_FIELD",
        "ATLASSIAN_WALDUR_BACKEND_ID_FIELD",
        "ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED",
        "ATLASSIAN_SHARED_USERNAME",
        "ATLASSIAN_VERIFY_SSL",
        "ATLASSIAN_USE_OLD_API",
        "ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS",
    ),
    "Zammad settings": (
        "ZAMMAD_API_URL",
        "ZAMMAD_TOKEN",
        "ZAMMAD_GROUP",
        "ZAMMAD_ARTICLE_TYPE",
        "ZAMMAD_COMMENT_MARKER",
        "ZAMMAD_COMMENT_PREFIX",
        "ZAMMAD_COMMENT_COOLDOWN_DURATION",
    ),
    "SMAX settings": (
        "SMAX_API_URL",
        "SMAX_TENANT_ID",
        "SMAX_LOGIN",
        "SMAX_PASSWORD",
        "SMAX_ORGANISATION_FIELD",
        "SMAX_PROJECT_FIELD",
        "SMAX_AFFECTED_RESOURCE_FIELD",
        "SMAX_REQUESTS_OFFERING",
        "SMAX_SECONDS_TO_WAIT",
        "SMAX_TIMES_TO_PULL",
        "SMAX_CREATION_SOURCE_NAME",
        "SMAX_VERIFY_SSL",
    ),
    "Proposal settings": (
        "PROPOSAL_REVIEW_DURATION",
        "REVIEWER_PROFILES_ENABLED",
        "COI_DETECTION_ENABLED",
        "COI_DISCLOSURE_REQUIRED",
        "AUTOMATED_MATCHING_ENABLED",
        "COI_COAUTHORSHIP_LOOKBACK_YEARS",
        "COI_COAUTHORSHIP_THRESHOLD_PAPERS",
        "COI_INSTITUTIONAL_LOOKBACK_YEARS",
    ),
    "ORCID integration settings": (
        "ORCID_CLIENT_ID",
        "ORCID_CLIENT_SECRET",
        "ORCID_REDIRECT_URI",
        "ORCID_API_URL",
        "ORCID_AUTH_URL",
        "ORCID_SANDBOX_MODE",
    ),
    "Publication API settings": (
        "SEMANTIC_SCHOLAR_API_KEY",
        "CROSSREF_MAILTO",
    ),
    "Table settings": ("USER_TABLE_COLUMNS",),
    "Localization": ("LANGUAGE_CHOICES",),
    "Authentication settings": (
        "AUTO_APPROVE_USER_TOS",
        "DEFAULT_IDP",
        "DEACTIVATE_USER_IF_NO_ROLES",
        "OIDC_BLOCK_CREATION_OF_UNINVITED_USERS",
        "OIDC_ACCESS_TOKEN_ENABLED",
    ),
    "Invitation settings": (
        "ENABLE_STRICT_CHECK_ACCEPTING_INVITATION",
        "INVITATION_DISABLE_MULTIPLE_ROLES",
        "INVITATION_ALLOWED_FIELDS",
    ),
    "User profile settings": (
        "DEFAULT_OFFERING_USER_ATTRIBUTES",
        "ENABLED_USER_PROFILE_ATTRIBUTES",
        "MANDATORY_USER_ATTRIBUTES",
        "ENFORCE_MANDATORY_USER_ATTRIBUTES",
    ),
    "Data privacy settings": (
        "USER_DATA_ACCESS_LOGGING_ENABLED",
        "USER_DATA_ACCESS_LOG_RETENTION_DAYS",
        "USER_DATA_ACCESS_LOG_SELF_ACCESS",
    ),
    "FreeIPA settings": (
        "FREEIPA_ENABLED",
        "FREEIPA_HOSTNAME",
        "FREEIPA_USERNAME",
        "FREEIPA_PASSWORD",
        "FREEIPA_VERIFY_SSL",
        "FREEIPA_USERNAME_PREFIX",
        "FREEIPA_GROUPNAME_PREFIX",
        "FREEIPA_BLACKLISTED_USERNAMES",
        "FREEIPA_GROUP_SYNCHRONIZATION_ENABLED",
    ),
    "API token authentication": (
        "OIDC_AUTH_URL",
        "OIDC_INTROSPECTION_URL",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_USER_FIELD",
        "OIDC_CACHE_TIMEOUT",
        "WALDUR_AUTH_SOCIAL_ROLE_CLAIM",
    ),
    "Onboarding settings": (
        "ONBOARDING_VALIDATION_METHODS",
        "ONBOARDING_VERIFICATION_EXPIRY_HOURS",
        "ONBOARDING_ARIREGISTER_BASE_URL",
        "ONBOARDING_ARIREGISTER_USERNAME",
        "ONBOARDING_ARIREGISTER_PASSWORD",
        "ONBOARDING_ARIREGISTER_TIMEOUT",
        "ONBOARDING_WICO_API_URL",
        "ONBOARDING_WICO_TOKEN",
        "ONBOARDING_BOLAGSVERKET_API_URL",
        "ONBOARDING_BOLAGSVERKET_TOKEN_API_URL",
        "ONBOARDING_BOLAGSVERKET_CLIENT_ID",
        "ONBOARDING_BOLAGSVERKET_CLIENT_SECRET",
        "ONBOARDING_BREG_API_URL",
    ),
    "LLM inference settings": (
        "LLM_CHAT_ENABLED",
        "LLM_INFERENCES_BACKEND_TYPE",
        "LLM_INFERENCES_API_URL",
        "LLM_INFERENCES_API_TOKEN",
        "LLM_INFERENCES_MODEL",
    ),
    "Software catalog settings": (
        "SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED",
        "SOFTWARE_CATALOG_EESSI_VERSION",
        "SOFTWARE_CATALOG_EESSI_API_URL",
        "SOFTWARE_CATALOG_EESSI_INCLUDE_EXTENSIONS",
        "SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED",
        "SOFTWARE_CATALOG_SPACK_VERSION",
        "SOFTWARE_CATALOG_SPACK_DATA_URL",
        "SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES",
        "SOFTWARE_CATALOG_CLEANUP_ENABLED",
        "SOFTWARE_CATALOG_RETENTION_DAYS",
    ),
    "Table Growth Monitoring": (
        "TABLE_GROWTH_MONITORING_ENABLED",
        "TABLE_GROWTH_WEEKLY_THRESHOLD_PERCENT",
        "TABLE_GROWTH_MONTHLY_THRESHOLD_PERCENT",
        "TABLE_GROWTH_RETENTION_DAYS",
        "TABLE_GROWTH_MIN_SIZE_BYTES",
    ),
    "User Actions": (
        "USER_ACTIONS_ENABLED",
        "USER_ACTIONS_PENDING_ORDER_HOURS",
        "USER_ACTIONS_HIGH_URGENCY_NOTIFICATION",
        "USER_ACTIONS_NOTIFICATION_THRESHOLD",
        "USER_ACTIONS_EXECUTION_RETENTION_DAYS",
        "USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS",
    ),
}

PUBLIC_CONSTANCE_SETTINGS = (
    # Whitelabeling settings
    "SITE_NAME",
    "SITE_DESCRIPTION",
    "SITE_ADDRESS",
    "SITE_EMAIL",
    "SITE_PHONE",
    "CURRENCY_NAME",
    "ANONYMOUS_USER_CAN_VIEW_OFFERINGS",
    "DOCS_URL",
    "SHORT_PAGE_TITLE",
    "FULL_PAGE_TITLE",
    "BRAND_COLOR",
    "HERO_LINK_LABEL",
    "HERO_LINK_URL",
    "SUPPORT_PORTAL_URL",
    "SITE_LOGO",
    "SIDEBAR_LOGO",
    "SIDEBAR_LOGO_MOBILE",
    "SIDEBAR_LOGO_DARK",
    "SIDEBAR_STYLE",
    "POWERED_BY_LOGO",
    "HERO_IMAGE",
    "MARKETPLACE_HERO_IMAGE",
    "CALL_MANAGEMENT_HERO_IMAGE",
    "LOGIN_LOGO",
    "FAVICON",
    "OFFERING_LOGO_PLACEHOLDER",
    "COMMON_FOOTER_TEXT",
    "COMMON_FOOTER_HTML",
    "LANGUAGE_CHOICES",
    "DISABLE_DARK_THEME",
    "LOGIN_PAGE_LAYOUT",
    "LOGIN_PAGE_VIDEO_URL",
    "LOGIN_PAGE_STATS",
    "LOGIN_PAGE_CAROUSEL_SLIDES",
    "LOGIN_PAGE_NEWS",
    "MARKETPLACE_LANDING_PAGE",
    "ENABLE_ORDER_START_DATE",
    "LLM_CHAT_ENABLED",
    # Support plugin
    "WALDUR_SUPPORT_ENABLED",
    "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE",
    "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE",
    "USER_TABLE_COLUMNS",
    # FreeIPA
    "FREEIPA_ENABLED",
    "FREEIPA_USERNAME_PREFIX",
    "DEFAULT_IDP",
    "HOMEPORT_URL",
    "KEYCLOAK_ICON",
    "RANCHER_USERNAME_INPUT_LABEL",
    "ENFORCE_USER_CONSENT_FOR_OFFERINGS",
    "OIDC_ACCESS_TOKEN_ENABLED",
    # Onboarding settings
    "ONBOARDING_VALIDATION_METHODS",
    # User Actions
    "USER_ACTIONS_ENABLED",
    # User profile attributes
    "ENABLED_USER_PROFILE_ATTRIBUTES",
    "MANDATORY_USER_ATTRIBUTES",
    "ENFORCE_MANDATORY_USER_ATTRIBUTES",
)
