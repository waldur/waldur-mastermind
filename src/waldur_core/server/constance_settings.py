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

SCRIPT_RUN_MODE_CHOICES = [
    ("docker", "Docker"),
    ("k8s", "Kubernetes"),
]

SIDEBAR_STYLE_CHOICES = [
    ("primary", "Primary"),
    ("accent", "Dark primary"),
    ("accent-light", "Light primary"),
    ("dark", "Dark"),
    ("light", "Light"),
    ("auto", "Match theme"),
]

FONT_FAMILY_CHOICES = [
    ("Inter", "Inter"),
    ("Maven Pro", "Maven Pro"),
]

MARKETPLACE_LAYOUT_MODE_CHOICES = [
    ("classic", "Classic"),
    ("sidebar", "Sidebar"),
    ("carousel", "Carousel"),
]

MARKETPLACE_CARD_STYLE_CHOICES = [
    ("compact", "Compact"),
    ("detailed", "Detailed"),
    ("list", "List"),
    ("minimal", "Minimal"),
]

LOGIN_PAGE_LAYOUT_CHOICES = [
    ("split-screen", "Split-screen"),
    ("centered-card", "Centered-card"),
    ("minimal", "Minimal"),
    ("full-hero", "Full-hero"),
    ("gradient", "Gradient"),
    ("stacked", "Stacked"),
    ("right-split", "Right-split"),
    ("glassmorphism", "Glassmorphism"),
    ("neumorphism", "Neumorphism"),
    ("animated-gradient", "Animated-gradient"),
    ("video-background", "Video-background"),
    ("bottom-sheet", "Bottom-sheet"),
    ("tabbed", "Tabbed"),
    ("wizard", "Wizard"),
    ("stats", "Stats"),
    ("news", "News"),
    ("carousel", "Carousel"),
    ("logo-watermark", "Logo-watermark"),
    ("brand-pattern", "Brand-pattern"),
    ("duotone", "Duotone"),
    ("diagonal", "Diagonal"),
    ("time-based", "Time-based"),
    ("seasonal", "Seasonal"),
    ("weather", "Weather"),
]

SUPPORT_BACKEND_CHOICES = [
    ("atlassian", "Atlassian"),
    ("zammad", "Zammad"),
    ("smax", "SMAX"),
]

ZAMMAD_ARTICLE_TYPE_CHOICES = [
    ("email", "email"),
    ("phone", "phone"),
    ("web", "web"),
    ("note", "note"),
    ("sms", "sms"),
    ("chat", "chat"),
    ("fax", "fax"),
    ("twitter status", "twitter status"),
    ("twitter direct-message", "twitter direct-message"),
    ("facebook feed post", "facebook feed post"),
    ("facebook feed comment", "facebook feed comment"),
    ("telegram personal-message", "telegram personal-message"),
]

OFFERING_VISIBILITY_CHOICES = [
    ("show_all", "Show all shared offerings"),
    ("show_restricted_disabled", "Show all but mark inaccessible as disabled"),
    ("hide_inaccessible", "Hide offerings user cannot access"),
    ("require_membership", "Hide all unless user belongs to an organization/project"),
]

AI_ASSISTANT_ENABLED_ROLES_CHOICES = [
    ("disabled", "Disabled"),
    ("staff", "Staff users"),
    ("staff_and_support", "Staff and support users"),
    ("all", "All users"),
    ("anonymous", "All users including anonymous"),
]

NOTIFY_SYSTEM_CHOICES = [
    ("AdminAnnouncement", "AdminAnnouncement"),
    ("BroadcastMessage", "BroadcastMessage"),
]

ONBOARDING_VALIDATION_CHOICES = [
    ("ariregister", "ariregister"),
    ("wirtschaftscompass", "wirtschaftscompass"),
    ("bolagsverket", "bolagsverket"),
]

DEACTIVATION_POLICY_CHOICES = [
    ("all_isds_removed", "All ISDs removed"),
    ("any_isd_removed", "Any ISD removal"),
]

SSH_KEY_TYPE_CHOICES = [
    ("ssh-ed25519", "ssh-ed25519"),
    ("ecdsa-sha2-nistp256", "ecdsa-sha2-nistp256"),
    ("ecdsa-sha2-nistp384", "ecdsa-sha2-nistp384"),
    ("ecdsa-sha2-nistp521", "ecdsa-sha2-nistp521"),
    ("ssh-rsa", "ssh-rsa"),
    ("sk-ssh-ed25519@openssh.com", "sk-ssh-ed25519@openssh.com"),
    ("sk-ecdsa-sha2-nistp256@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"),
]

PROVIDER_CHOICES = [
    ("", "Not configured"),
    ("tara", "TARA"),
    ("eduteams", "eduTEAMS"),
    ("keycloak", "Keycloak"),
]

# Note: These should ideally be imported from marketplace, but for now we define common ones.
OFFERING_TYPE_CHOICES = [
    ("Support.OfferingTemplate", "Support"),
    ("Marketplace.Booking", "Booking"),
    ("Marketplace.Basic", "Basic"),
    ("OpenStack.Tenant", "OpenStack Tenant"),
    ("OpenStack.Instance", "OpenStack Instance"),
    ("OpenStack.Volume", "OpenStack Volume"),
    ("Marketplace.Rancher", "Rancher"),
    ("VMware.VirtualMachine", "VMware Virtual Machine"),
    ("Waldur.RemoteOffering", "Remote Offering"),
    ("Marketplace.Script", "Script"),
    ("SlurmInvoices.SlurmPackage", "SLURM Package"),
    ("Marketplace.Slurm", "Site Agent"),
]

USER_ATTRIBUTE_CHOICES = [
    ("username", "Username"),
    ("registration_method", "Registration method"),
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("full_name", "Full name"),
    ("email", "Email"),
    ("phone_number", "Phone number"),
    ("organization", "Organization"),
    ("job_title", "Job title"),
    ("affiliations", "Affiliations"),
    ("gender", "Gender"),
    ("personal_title", "Personal title"),
    ("birth_date", "Birth date"),
    ("place_of_birth", "Place of birth"),
    ("country_of_residence", "Country of residence"),
    ("nationality", "Nationality"),
    ("nationalities", "Nationalities"),
    ("organization_country", "Organization country"),
    ("organization_type", "Organization type"),
    ("organization_registry_code", "Organization registry code"),
    ("eduperson_assurance", "Eduperson assurance"),
    ("civil_number", "Civil number"),
    ("identity_source", "Identity source"),
]

REPORTING_SCREEN_CHOICES = [
    # Resources
    ("resource-usage", "Resources: Usage"),
    ("user-usage", "Resources: Usage by user"),
    ("quotas", "Resources: Quotas"),
    ("usage-monitoring", "Resources: Usage monitoring"),
    ("usage-trends", "Resources: Usage trends"),
    ("organization-summary", "Resources: Organization summary"),
    ("project-detail", "Resources: Project detail"),
    ("resources-geography", "Resources: Geographic distribution"),
    ("project-classification", "Resources: Project classification"),
    ("usage-by-customer", "Resources: Usage by customer"),
    ("usage-by-org-type", "Resources: Usage by organization type"),
    ("usage-by-creator", "Resources: Usage by creator"),
    # Proposals
    ("call-performance", "Proposals: Call performance"),
    ("review-progress", "Proposals: Review progress"),
    ("resource-demand", "Proposals: Resource demand"),
    # Provider
    ("capacity", "Provider: Capacity"),
    ("provider-overview", "Provider: Provider overview"),
    ("provider-revenue", "Provider: Provider revenue"),
    ("provider-orders", "Provider: Provider orders"),
    ("provider-resources", "Provider: Provider resources"),
    ("provider-customers", "Provider: Provider customers"),
    ("provider-offerings", "Provider: Provider offerings"),
    ("openstack-instances", "Provider: OpenStack instances"),
    ("offering-usage", "Provider: Offering component usage"),
    # Users
    ("user-analytics", "Users: Analytics"),
    ("user-demographics", "Users: Demographics"),
    ("user-organizations", "Users: Organizations"),
    ("user-affiliations", "Users: Affiliations"),
    ("user-roles", "Users: Role distribution"),
    # Financial
    ("growth", "Financial: Growth"),
    ("revenue", "Financial: Monthly revenue"),
    ("pricelist", "Financial: Pricelist"),
    ("orders", "Financial: Orders"),
    ("offering-costs", "Financial: Offering costs"),
    # Operations
    ("maintenance-overview", "Operations: Maintenance overview"),
    ("provisioning-stats", "Operations: Provisioning statistics"),
]

DEFAULT_ENABLED_REPORTING_SCREENS = [key for key, _ in REPORTING_SCREEN_CHOICES]

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
    "multiple_choice_field": [
        "waldur_core.core.serializers.ListField",
        {"required": False},
    ],
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

CONSTANCE_CONFIG_CHOICES = {
    "SCRIPT_RUN_MODE": SCRIPT_RUN_MODE_CHOICES,
    "DEFAULT_IDP": PROVIDER_CHOICES,
    "SIDEBAR_STYLE": SIDEBAR_STYLE_CHOICES,
    "FONT_FAMILY": FONT_FAMILY_CHOICES,
    "LOGIN_PAGE_LAYOUT": LOGIN_PAGE_LAYOUT_CHOICES,
    "MARKETPLACE_LAYOUT_MODE": MARKETPLACE_LAYOUT_MODE_CHOICES,
    "MARKETPLACE_CARD_STYLE": MARKETPLACE_CARD_STYLE_CHOICES,
    "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": SUPPORT_BACKEND_CHOICES,
    "ZAMMAD_ARTICLE_TYPE": ZAMMAD_ARTICLE_TYPE_CHOICES,
    "DEFAULT_OFFERING_USER_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
    "DEFAULT_CALL_USER_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
    "INVITATION_ALLOWED_FIELDS": USER_ATTRIBUTE_CHOICES,
    "ENABLED_USER_PROFILE_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
    "MANDATORY_USER_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
    "MAINTENANCE_ANNOUNCEMENT_NOTIFY_SYSTEM": NOTIFY_SYSTEM_CHOICES,
    "DISABLED_OFFERING_TYPES": OFFERING_TYPE_CHOICES,
    "ONBOARDING_VALIDATION_METHODS": ONBOARDING_VALIDATION_CHOICES,
    "FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
    "FEDERATED_IDENTITY_DEACTIVATION_POLICY": DEACTIVATION_POLICY_CHOICES,
    "SCIM_INBOUND_ALLOWED_ATTRIBUTES": USER_ATTRIBUTE_CHOICES,
    "RESTRICTED_OFFERING_VISIBILITY_MODE": OFFERING_VISIBILITY_CHOICES,
    "SSH_KEY_ALLOWED_TYPES": SSH_KEY_TYPE_CHOICES,
    "ENABLED_REPORTING_SCREENS": REPORTING_SCREEN_CHOICES,
    "AI_ASSISTANT_ENABLED_ROLES": AI_ASSISTANT_ENABLED_ROLES_CHOICES,
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
    "DISCLAIMER_AREA_TEXT": (
        "",
        "Text content rendered in the disclaimer area below the footer.",
        "text_field",
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
    "ENABLE_MARKDOWN_IMAGE_UPLOAD": (
        False,
        "Allow uploading images for embedding in offering markdown descriptions.",
    ),
    "MARKDOWN_IMAGE_MAX_SIZE_MB": (
        5,
        "Maximum size in megabytes for a markdown image upload.",
    ),
    "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": (
        True,
        "Allow anonymous users to see shared offerings in active, paused and archived states",
    ),
    "SHOW_OFFERING_COVER_IMAGE": (
        False,
        "Show offering cover image as a banner above the name on the offering page.",
    ),
    "ANONYMOUS_USER_CAN_VIEW_PLANS": (True, "Allow anonymous users to see plans"),
    "RESTRICTED_OFFERING_VISIBILITY_MODE": (
        "show_all",
        "Controls offering visibility for regular users. "
        "'show_all': Show all shared offerings (current behavior). "
        "'show_restricted_disabled': Show all but mark inaccessible as disabled. "
        "'hide_inaccessible': Hide offerings user cannot access. "
        "'require_membership': Hide all unless user belongs to an organization/project.",
        "choice_field",
    ),
    "ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT": (
        False,
        "If true, service provider owners and managers can manage offering lifecycle (activate, pause, unpause, archive, draft, delete) without staff approval.",
    ),
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
    "MARKETPLACE_LAYOUT_MODE": (
        "classic",
        "Default marketplace layout mode.",
        "choice_field",
    ),
    "MARKETPLACE_CARD_STYLE": (
        "detailed",
        "Default marketplace offering card style.",
        "choice_field",
    ),
    "ENABLE_STALE_RESOURCE_NOTIFICATIONS": (
        False,
        "Enable reminders to owners about resources of shared offerings that have not generated any cost for the last 3 months.",
    ),
    "ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES": (
        False,
        "If true, a support ticket is created when a user adds or removes an SSH public key.",
    ),
    "TELEMETRY_URL": (
        "https://telemetry.waldur.com/",
        "URL for sending telemetry data.",
    ),
    "TELEMETRY_VERSION": (1, "Telemetry service version."),
    "SCRIPT_RUN_MODE": (
        "docker",
        'Type of jobs deployment. Valid values: "docker" for simple docker deployment, "k8s" for Kubernetes-based one',
        "choice_field",
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
            "python": {"image": "python:3.12-alpine", "command": "python"},
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
    "DEFAULT_IDP": (
        "",
        "Triggers authentication flow at once.",
        "choice_field",
    ),
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
    "AFFILIATION_REQUIRED_AT_PROJECT_CREATION": (
        False,
        "If true, the affiliation field is required when creating or updating projects.",
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
        "Style of sidebar.",
        "choice_field",
    ),
    "FONT_FAMILY": (
        "Inter",
        "Font family used in the UI.",
        "choice_field",
    ),
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
        "Login page layout style.",
        "choice_field",
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
    "DISCLAIMER_AREA_LOGO": (
        "",
        "The logo image rendered in the disclaimer area below the footer.",
        "image_field",
    ),
    # service desk integration settings
    "WALDUR_SUPPORT_ENABLED": (
        True,
        "Toggler for support plugin.",
    ),
    "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": (
        "atlassian",
        "Type of support backend.",
        "choice_field",
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
    "JIRA_WEBHOOK_SHARED_SECRET": (
        "",
        "Shared secret expected in the X-Webhook-Secret header of inbound "
        "JIRA webhook deliveries. If empty, authentication is not enforced "
        "and the receiver accepts unauthenticated requests (legacy "
        "behaviour). Configure your JIRA automation/webhook to send the "
        "same value to enable authentication.",
        "secret_field",
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
        "Type of a comment.",
        "choice_field",
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
    "ZAMMAD_WEBHOOK_SHARED_SECRET": (
        "",
        "Shared secret expected in the X-Webhook-Secret header of inbound "
        "Zammad webhook deliveries. If empty, authentication is not "
        "enforced and the receiver accepts unauthenticated requests "
        "(legacy behaviour).",
        "secret_field",
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
    "SMAX_WEBHOOK_SHARED_SECRET": (
        "",
        "Shared secret expected in the X-Webhook-Secret header of inbound "
        "SMAX webhook deliveries. If empty, authentication is not enforced "
        "and the receiver accepts unauthenticated requests (legacy "
        "behaviour).",
        "secret_field",
    ),
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
    "SCIM_MEMBERSHIP_SYNC_ENABLED": (
        False,
        "Enable SCIM entitlement synchronization to external identity provider.",
    ),
    "SCIM_API_URL": ("", "Base URL of the SCIM API service."),
    "SCIM_API_KEY": ("", "SCIM API key for X-API-Key header.", "secret_field"),
    "SCIM_URN_NAMESPACE": ("", "URN namespace for SCIM entitlements."),
    "SCIM_INBOUND_ENABLED": (
        False,
        "Enable inbound SCIM 2.0 service provider at /scim/v2/. Allows external "
        "identity providers (Okta, Entra ID, Keycloak) to provision users and groups.",
    ),
    "SCIM_INBOUND_SOURCE_NAME": (
        "scim:default",
        "Source label written to User.attribute_sources for inbound SCIM writes. "
        "Used by the multi-source attribute merge to track ownership.",
    ),
    "SCIM_INBOUND_ALLOWED_ATTRIBUTES": (
        ["first_name", "last_name", "email", "organization", "affiliations"],
        "User attributes settable via inbound SCIM.",
        "multiple_choice_field",
    ),
    "SCIM_PULL_API_URL": (
        "",
        "Base URL for outbound SCIM pull (fetching user attributes from an external IdP).",
    ),
    "SCIM_PULL_API_KEY": (
        "",
        "Bearer token for outbound SCIM pull.",
        "secret_field",
    ),
    "SCIM_PULL_SOURCE_NAME": (
        "scim:pull",
        "Source label written to User.attribute_sources for attributes pulled from a remote SCIM directory.",
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
        "secret_field",
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
        "If true, block creation of an account on OIDC login if user email is not provided or provided and is not in the list of one of the active invitations or matching active group invitation email patterns.",
    ),
    "OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE": (
        "Account creation is blocked for uninvited users.",
        "The message to show when OIDC account creation is blocked for uninvited users.",
        "text_field",
    ),
    "OIDC_MATCHMAKING_BY_EMAIL": (
        False,
        "If true, when OIDC login fails to find a user by the primary lookup field, "
        "attempt a secondary lookup by email before creating a new user. "
        "On successful email match, the user's primary lookup field is updated to the OIDC claim value.",
    ),
    "OIDC_DEFAULT_LOGOUT_URL": (
        "",
        "Default logout URL used as fallback when IdentityProvider does not have a logout_url set. "
        "This allows configuring a global logout endpoint for OIDC providers that don't expose end_session_endpoint in their discovery document.",
        "url_field",
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
    "REMOTE_EDUTEAMS_REFRESH_TOKEN": (
        "",
        "Rotating OAuth2 refresh token for remote eduTEAMS API access. "
        "Automatically updated by the periodic token rotation task. "
        "If empty, falls back to REMOTE_EDUTEAMS_REFRESH_TOKEN from Django settings.",
        "secret_field",
    ),
    "DEFAULT_OFFERING_USER_ATTRIBUTES": (
        ["username", "full_name", "email"],
        "Default user attributes exposed to service providers (OfferingUser API) when no explicit config exists.",
        "multiple_choice_field",
    ),
    "DEFAULT_CALL_USER_ATTRIBUTES": (
        ["username", "full_name", "email"],
        "Default applicant attributes exposed to call reviewers when no explicit CallApplicantVisibilityConfig exists.",
        "multiple_choice_field",
    ),
    "INVITATION_ALLOWED_FIELDS": (
        ["full_name", "organization", "job_title"],
        "Fields that can be provided in invitations for email personalization. These are NOT copied to user profile.",
        "multiple_choice_field",
    ),
    "ENABLED_USER_PROFILE_ATTRIBUTES": (
        ["phone_number", "organization", "job_title", "affiliations"],
        "List of enabled user profile attributes. Controls IdP sync and UI display.",
        "multiple_choice_field",
    ),
    "MANDATORY_USER_ATTRIBUTES": (
        [],
        "List of user profile attributes that are mandatory.",
        "multiple_choice_field",
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
        "How maintenance notifications are delivered.",
        "multiple_choice_field",
    ),
    "ENFORCE_USER_CONSENT_FOR_OFFERINGS": (
        False,
        "If True, users must have active consent to access offerings that have active Terms of Service.",
    ),
    "ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS": (
        False,
        "If True, service providers only see offering users whose profiles have "
        "all exposed attributes filled (per OfferingUserAttributeConfig).",
    ),
    "DISABLED_OFFERING_TYPES": (
        [],
        "List of offering types disabled for creation and selection.",
        "multiple_choice_field",
    ),
    "ONBOARDING_VALIDATION_METHODS": (
        [],
        "List of automatic validation methods available for this portal.",
        "multiple_choice_field",
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
    # AI assistant settings
    "AI_ASSISTANT_ENABLED": (
        False,
        "Enable AI Assistant feature and calls to the inference service.",
    ),
    "AI_ASSISTANT_ENABLED_ROLES": (
        "disabled",
        "Controls which user roles can access the AI Assistant. "
        "'disabled': No role-based access. "
        "'staff': Staff users only. "
        "'staff_and_support': Staff and support users. "
        "'all': All authenticated users. "
        "'anonymous': All users including anonymous (enables the public anonymous chat endpoint).",
        "choice_field",
    ),
    "AI_ASSISTANT_BACKEND_TYPE": (
        "vllm",
        "Type of AI Assistant backend. For example: vllm, openai, ollama.",
    ),
    "AI_ASSISTANT_API_URL": (
        "",
        "Base URL for AI Assistant service API.",
        "url_field",
    ),
    "AI_ASSISTANT_API_TOKEN": (
        "",
        "API key for authenticating with the AI Assistant service.",
        "secret_field",
    ),
    "AI_ASSISTANT_MODEL": (
        "qwen3.5-122b-nothinking",
        "Name of the AI Assistant model to use for inference.",
    ),
    "AI_ASSISTANT_COMPLETION_KWARGS": (
        {},
        "Override keyword arguments merged on top of provider defaults for AI Assistant chat completion. "
        "Supported keys: temperature, top_p, top_k, max_tokens, max_completion_tokens, "
        "presence_penalty, frequency_penalty, repetition_penalty, stop, seed, "
        "reasoning_effort, extra_body. "
        "Leave empty to use provider defaults.",
        "dict_field",
    ),
    "AI_ASSISTANT_TOKEN_LIMIT_DAILY": (
        -1,
        "Per-actor daily token cap (authenticated OR anonymous). -1 means unlimited.",
    ),
    "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY": (
        -1,
        "Per-actor (authenticated OR anonymous) weekly token cap. -1 means unlimited.",
    ),
    "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY": (
        -1,
        "Per-actor (authenticated OR anonymous) monthly token cap. -1 means unlimited.",
    ),
    "AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET": (
        5000000,
        "Site-wide daily token cap across all assistant traffic (auth + "
        "anonymous). -1 means unlimited.",
    ),
    "AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE": (
        60,
        "Site-wide burst cap across all assistant traffic.",
    ),
    "AI_ASSISTANT_SESSION_RETENTION_DAYS": (
        90,
        "Number of days to retain AI Assistant sessions before automatic deletion. Set to -1 to disable automatic cleanup.",
    ),
    "AI_ASSISTANT_HISTORY_LIMIT": (
        50,
        "Maximum number of past messages included in the AI Assistant context window.",
    ),
    "AI_ASSISTANT_STREAM_TIMEOUT_SECONDS": (
        120,
        "Hard timeout in seconds for a full streaming request including LLM completion.",
    ),
    "AI_ASSISTANT_INJECTION_ALLOWLIST": (
        "",
        "Comma-separated allowlist phrases that bypass injection detection.",
    ),
    "AI_ASSISTANT_NAME": (
        "Waldur Assistant",
        "Display name for the AI Assistant persona (e.g. 'Mari', 'Waldur Assistant').",
    ),
    "AI_ASSISTANT_SYSTEM_PROMPT_CUSTOM_INSTRUCTIONS": (
        "",
        "Additional instructions injected into the AI Assistant system prompt. "
        "Use this for organisation-specific context, terminology, FAQ content, "
        "or behavioural guidelines. Supports {assistant_name} and {organization} "
        "placeholders. Overridden by the active SystemPrompt record when set.",
        "text_field",
    ),
    # Anonymous AI assistant settings
    "ANONYMOUS_CHAT_USER_SLUG_SALT": (
        "",
        "Scrypt salt for per-IP user_slug derivation. Empty disables slug "
        "computation (interactions are written without it).",
        "secret_field",
    ),
    "ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET": (
        "",
        "HMAC-SHA256 secret for /feedback/ anti-replay tokens. Loss of "
        "secrecy invalidates all in-flight feedback submissions.",
        "secret_field",
    ),
    "ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES": (
        50,
        "Hard cap on the number of offerings injected into the anonymous "
        "assistant's system prompt catalog summary. Past this, drop the tail.",
    ),
    "ANONYMOUS_CHAT_REVIEW_ENABLED": (
        True,
        "Master toggle for the nightly LLM-as-judge review of completed "
        "anonymous sessions. On by default — cost is bounded by "
        "ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET.",
    ),
    "ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET": (
        2000000,
        "Independent budget for the LLM judge so review can't starve "
        "user-facing traffic. Reuses AI_ASSISTANT_API_URL/TOKEN/MODEL.",
    ),
    "ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS": (
        30,
        "Days of inactivity after which pseudonymous bookkeeping rows "
        "(SessionBinding, AnonymousChatBudget) are purged. Active blocks "
        "are always retained until they expire. Set to -1 to disable.",
    ),
    # Software catalog settings
    "SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED": (
        False,
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
        False,
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
    # System Logging settings
    "SYSTEM_LOG_ENABLED": (
        False,
        "Enable storing system logs (API, Worker, Beat) in the database for staff viewing.",
    ),
    "SYSTEM_LOG_MAX_ROWS_PER_SOURCE": (
        5000,
        "Maximum number of log rows to keep per source (api, worker, beat). Oldest rows are deleted when exceeded.",
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
        False,
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
    # OpenStack call tracing settings
    "OPENSTACK_LOG_CALLS_ENABLED": (
        False,
        "Emit one log line per OpenStack HTTP call on logger "
        "`waldur_openstack.calls` (method, host+path, status, elapsed ms, "
        "originating backend action). Useful for diagnosing slow tenant "
        "operations; off by default because chatty under steady-state load.",
    ),
    "OPENSTACK_LOG_CALLS_THRESHOLD_MS": (
        0,
        "When OPENSTACK_LOG_CALLS_ENABLED is on, only emit lines for calls "
        "slower than this many milliseconds. 0 logs every call. Errors are "
        "always logged regardless of this threshold.",
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
    # Arrow Integration Settings
    "ARROW_AUTO_RECONCILIATION": (
        False,
        "Auto-apply compensations when Arrow validates billing",
    ),
    "ARROW_SYNC_INTERVAL_HOURS": (
        6,
        "Billing sync interval in hours",
    ),
    "ARROW_CONSUMPTION_SYNC_ENABLED": (
        False,
        "Enable real-time consumption sync from Arrow API",
    ),
    "ARROW_CONSUMPTION_SYNC_INTERVAL_HOURS": (
        1,
        "Consumption sync interval in hours (default: hourly)",
    ),
    "ARROW_BILLING_CHECK_INTERVAL_HOURS": (
        6,
        "Billing export check interval in hours for reconciliation",
    ),
    # Usage polling settings
    "USAGE_POLL_RECORD_RETENTION_MONTHS": (
        3,
        "Number of months to retain usage poll records before automatic cleanup.",
    ),
    # SLURM Policy settings
    "SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS": (
        90,
        "Number of days to retain SLURM policy evaluation log entries before automatic cleanup.",
    ),
    # Identity Bridge settings
    "FEDERATED_IDENTITY_SYNC_ENABLED": (
        False,
        "Enable the Identity Bridge API for push-based ISD user attribute synchronization.",
    ),
    "FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES": (
        ["first_name", "last_name", "email", "organization", "affiliations"],
        "User attributes settable via Identity Bridge.",
        "multiple_choice_field",
    ),
    "FEDERATED_IDENTITY_DEACTIVATION_POLICY": (
        "any_isd_removed",
        "When to deactivate a federated user.",
        "choice_field",
    ),
    # Project Digest settings
    "ENABLE_PROJECT_DIGEST": (
        False,
        "Enable project digest email notifications for organizations.",
    ),
    # SSH key settings
    "SSH_KEY_ALLOWED_TYPES": (
        [
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "ssh-rsa",
            "sk-ssh-ed25519@openssh.com",
            "sk-ecdsa-sha2-nistp256@openssh.com",
        ],
        "List of allowed SSH key types. Empty list means all types are allowed.",
        "multiple_choice_field",
    ),
    "SSH_KEY_MIN_RSA_KEY_SIZE": (
        2048,
        "Minimum allowed RSA key size in bits. Set to 0 to disable the check.",
    ),
    "ENABLED_REPORTING_SCREENS": (
        DEFAULT_ENABLED_REPORTING_SCREENS,
        "Select which reporting screens should be visible to users. Uncheck to disable specific reports.",
        "multiple_choice_field",
    ),
    # Site Agent Logs
    "SITE_AGENT_LOG_MAX_ROWS_PER_IDENTITY": (
        10000,
        "Maximum number of log rows to keep per agent identity. Oldest rows are deleted when exceeded.",
    ),
    # Personal Access Tokens
    "PAT_ENABLED": (
        False,
        "Enable Personal Access Token authentication.",
    ),
    "PAT_MAX_LIFETIME_DAYS": (
        365,
        "Maximum PAT lifetime in days.",
    ),
    "PAT_MAX_TOKENS_PER_USER": (
        20,
        "Maximum number of active PATs per user.",
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
        "DISCLAIMER_AREA_TEXT",
    ),
    "Marketplace Branding": (
        "SITE_ADDRESS",
        "SITE_EMAIL",
        "SITE_PHONE",
        "CURRENCY_NAME",
        "MARKETPLACE_LANDING_PAGE",
        "MARKETPLACE_LAYOUT_MODE",
        "MARKETPLACE_CARD_STYLE",
        "COUNTRIES",
    ),
    "Marketplace visibility & access": (
        "ANONYMOUS_USER_CAN_VIEW_OFFERINGS",
        "ANONYMOUS_USER_CAN_VIEW_PLANS",
        "RESTRICTED_OFFERING_VISIBILITY_MODE",
        "SHOW_OFFERING_COVER_IMAGE",
        "ENABLE_MARKDOWN_IMAGE_UPLOAD",
        "ENFORCE_USER_CONSENT_FOR_OFFERINGS",
        "ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS",
        "ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT",
    ),
    "Marketplace notifications": (
        "NOTIFY_STAFF_ABOUT_APPROVALS",
        "NOTIFY_ABOUT_RESOURCE_CHANGE",
        "DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE",
        "ENABLE_STALE_RESOURCE_NOTIFICATIONS",
    ),
    "Offerings & orders": (
        "THUMBNAIL_SIZE",
        "ENABLE_MARKDOWN_IMAGE_UPLOAD",
        "MARKDOWN_IMAGE_MAX_SIZE_MB",
        "DISABLED_OFFERING_TYPES",
        "ENABLE_ORDER_START_DATE",
    ),
    "Marketplace development": (
        "ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND",
        "ENABLE_MOCK_COURSE_ACCOUNT_BACKEND",
    ),
    "Project": (
        "PROJECT_END_DATE_MANDATORY",
        "AFFILIATION_REQUIRED_AT_PROJECT_CREATION",
    ),
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
        "FONT_FAMILY",
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
        "DISCLAIMER_AREA_LOGO",
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
        "JIRA_WEBHOOK_SHARED_SECRET",
    ),
    "Zammad settings": (
        "ZAMMAD_API_URL",
        "ZAMMAD_TOKEN",
        "ZAMMAD_GROUP",
        "ZAMMAD_ARTICLE_TYPE",
        "ZAMMAD_COMMENT_MARKER",
        "ZAMMAD_COMMENT_PREFIX",
        "ZAMMAD_COMMENT_COOLDOWN_DURATION",
        "ZAMMAD_WEBHOOK_SHARED_SECRET",
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
        "SMAX_WEBHOOK_SHARED_SECRET",
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
        "OIDC_BLOCK_CREATION_OF_UNINVITED_USERS_RESPONSE_MESSAGE",
        "OIDC_MATCHMAKING_BY_EMAIL",
        "OIDC_ACCESS_TOKEN_ENABLED",
        "REMOTE_EDUTEAMS_REFRESH_TOKEN",
    ),
    "Invitation settings": (
        "ENABLE_STRICT_CHECK_ACCEPTING_INVITATION",
        "INVITATION_DISABLE_MULTIPLE_ROLES",
        "INVITATION_ALLOWED_FIELDS",
    ),
    "User profile settings": (
        "DEFAULT_OFFERING_USER_ATTRIBUTES",
        "DEFAULT_CALL_USER_ATTRIBUTES",
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
    "SCIM Entitlements (outbound push)": (
        "SCIM_MEMBERSHIP_SYNC_ENABLED",
        "SCIM_API_URL",
        "SCIM_API_KEY",
        "SCIM_URN_NAMESPACE",
    ),
    "SCIM Identity Provider": (
        "SCIM_INBOUND_ENABLED",
        "SCIM_INBOUND_SOURCE_NAME",
        "SCIM_INBOUND_ALLOWED_ATTRIBUTES",
        "SCIM_PULL_API_URL",
        "SCIM_PULL_API_KEY",
        "SCIM_PULL_SOURCE_NAME",
    ),
    "API token authentication": (
        "OIDC_AUTH_URL",
        "OIDC_INTROSPECTION_URL",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_USER_FIELD",
        "OIDC_CACHE_TIMEOUT",
        "OIDC_DEFAULT_LOGOUT_URL",
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
    "AI assistant settings": (
        "AI_ASSISTANT_NAME",
        "AI_ASSISTANT_ENABLED",
        "AI_ASSISTANT_ENABLED_ROLES",
        "AI_ASSISTANT_BACKEND_TYPE",
        "AI_ASSISTANT_API_URL",
        "AI_ASSISTANT_API_TOKEN",
        "AI_ASSISTANT_MODEL",
        "AI_ASSISTANT_SYSTEM_PROMPT_CUSTOM_INSTRUCTIONS",
        "AI_ASSISTANT_COMPLETION_KWARGS",
        "AI_ASSISTANT_STREAM_TIMEOUT_SECONDS",
        "AI_ASSISTANT_TOKEN_LIMIT_DAILY",
        "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY",
        "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY",
        "AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET",
        "AI_ASSISTANT_GLOBAL_REQUESTS_PER_MINUTE",
        "AI_ASSISTANT_SESSION_RETENTION_DAYS",
        "AI_ASSISTANT_HISTORY_LIMIT",
        "AI_ASSISTANT_INJECTION_ALLOWLIST",
        "ANONYMOUS_CHAT_USER_SLUG_SALT",
        "ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET",
        "ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES",
        "ANONYMOUS_CHAT_REVIEW_ENABLED",
        "ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET",
        "ANONYMOUS_CHAT_ARTIFACT_RETENTION_DAYS",
    ),
    "Software catalog general": (
        "SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES",
        "SOFTWARE_CATALOG_CLEANUP_ENABLED",
        "SOFTWARE_CATALOG_RETENTION_DAYS",
    ),
    "Software catalog EESSI": (
        "SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED",
        "SOFTWARE_CATALOG_EESSI_VERSION",
        "SOFTWARE_CATALOG_EESSI_API_URL",
        "SOFTWARE_CATALOG_EESSI_INCLUDE_EXTENSIONS",
    ),
    "Software catalog Spack": (
        "SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED",
        "SOFTWARE_CATALOG_SPACK_VERSION",
        "SOFTWARE_CATALOG_SPACK_DATA_URL",
    ),
    "System Logging": (
        "SYSTEM_LOG_ENABLED",
        "SYSTEM_LOG_MAX_ROWS_PER_SOURCE",
        "OPENSTACK_LOG_CALLS_ENABLED",
        "OPENSTACK_LOG_CALLS_THRESHOLD_MS",
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
    "Arrow Integration": (
        "ARROW_AUTO_RECONCILIATION",
        "ARROW_SYNC_INTERVAL_HOURS",
        "ARROW_CONSUMPTION_SYNC_ENABLED",
        "ARROW_CONSUMPTION_SYNC_INTERVAL_HOURS",
        "ARROW_BILLING_CHECK_INTERVAL_HOURS",
    ),
    "SLURM Policy": ("SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS",),
    "Usage Polling": ("USAGE_POLL_RECORD_RETENTION_MONTHS",),
    "Identity Bridge": (
        "FEDERATED_IDENTITY_SYNC_ENABLED",
        "FEDERATED_IDENTITY_SYNC_ALLOWED_ATTRIBUTES",
        "FEDERATED_IDENTITY_DEACTIVATION_POLICY",
    ),
    "Project Digest": ("ENABLE_PROJECT_DIGEST",),
    "SSH keys": (
        "SSH_KEY_ALLOWED_TYPES",
        "SSH_KEY_MIN_RSA_KEY_SIZE",
        "ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES",
    ),
    "Reporting": ("ENABLED_REPORTING_SCREENS",),
    "Personal Access Tokens": (
        "PAT_ENABLED",
        "PAT_MAX_LIFETIME_DAYS",
        "PAT_MAX_TOKENS_PER_USER",
    ),
    "Site Agent Logs": ("SITE_AGENT_LOG_MAX_ROWS_PER_IDENTITY",),
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
    "SHOW_OFFERING_COVER_IMAGE",
    "ENABLE_MARKDOWN_IMAGE_UPLOAD",
    "RESTRICTED_OFFERING_VISIBILITY_MODE",
    "DOCS_URL",
    "SHORT_PAGE_TITLE",
    "FULL_PAGE_TITLE",
    "BRAND_COLOR",
    "HERO_LINK_LABEL",
    "HERO_LINK_URL",
    "SUPPORT_PORTAL_URL",
    "SIDEBAR_LOGO",
    "SIDEBAR_LOGO_MOBILE",
    "SIDEBAR_LOGO_DARK",
    "SIDEBAR_STYLE",
    "FONT_FAMILY",
    "POWERED_BY_LOGO",
    "HERO_IMAGE",
    "MARKETPLACE_HERO_IMAGE",
    "CALL_MANAGEMENT_HERO_IMAGE",
    "LOGIN_LOGO",
    "FAVICON",
    "OFFERING_LOGO_PLACEHOLDER",
    "DISCLAIMER_AREA_LOGO",
    "DISCLAIMER_AREA_TEXT",
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
    "MARKETPLACE_LAYOUT_MODE",
    "MARKETPLACE_CARD_STYLE",
    "ENABLE_ORDER_START_DATE",
    "ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT",
    "AI_ASSISTANT_ENABLED",
    "AI_ASSISTANT_ENABLED_ROLES",
    "AI_ASSISTANT_NAME",
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
    "ENFORCE_OFFERING_USER_PROFILE_COMPLETENESS",
    # Project Digest
    "ENABLE_PROJECT_DIGEST",
    # SSH keys
    "SSH_KEY_ALLOWED_TYPES",
    "SSH_KEY_MIN_RSA_KEY_SIZE",
    "ENABLED_REPORTING_SCREENS",
    # Personal Access Tokens
    "PAT_ENABLED",
)
