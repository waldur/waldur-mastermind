"""
Django base settings for Waldur Core.
"""

import locale

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
import os
import warnings
from datetime import timedelta

from waldur_core.core import WaldurExtension
from waldur_core.core.metadata import WaldurConfiguration
from waldur_core.server.admin.settings import *  # noqa: F403

encoding = locale.getpreferredencoding()
if encoding.lower() != "utf-8":
    raise Exception(
        """Your system's preferred encoding is `{}`, but Waldur requires `UTF-8`.
Fix it by setting the LC_* and LANG environment settings. Example:
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
""".format(encoding)
    )

ADMINS = ()

BASE_DIR = os.path.abspath(
    os.path.join(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."), "..")
)

DEBUG = False

MEDIA_ROOT = "/media_root/"

MEDIA_URL = "/media/"

ALLOWED_HOSTS = []
SITE_ID = 1
DBTEMPLATES_USE_REVERSION = True
DBTEMPLATES_USE_CODEMIRROR = True

# Application definition
INSTALLED_APPS = (
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.humanize",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "waldur_core.landing",
    "waldur_core.logging",
    "waldur_core.core",
    "waldur_core.permissions",
    "waldur_core.quotas",
    "waldur_core.structure",
    "waldur_core.users",
    "waldur_core.media",
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_swagger",
    "django_filters",
    "axes",
    "django_fsm",
    "reversion",
    "jsoneditor",
    "modeltranslation",
    "health_check",
    "health_check.db",
    "health_check.cache",
    "health_check.storage",
    "health_check.contrib.migrations",
    "health_check.contrib.celery_ping",
    "dbtemplates",
    "binary_database_files",
    "netfields",
    "constance",
    "constance.backends.database",
)
INSTALLED_APPS += ADMIN_INSTALLED_APPS  # noqa: F405

MIDDLEWARE = (
    "waldur_core.media.middleware.attachment_middleware",
    "waldur_core.server.middleware.cors_middleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "waldur_core.logging.middleware.CaptureEventContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
    "waldur_core.server.middleware.ImpersonationMiddleware",
)

REST_FRAMEWORK = {
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "waldur_core.core.authentication.TokenAuthentication",
        "waldur_core.core.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "waldur_core.core.renderers.BrowsableAPIRenderer",
    ),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "oauth": "10/s",
    },
    "DEFAULT_PAGINATION_CLASS": "waldur_core.core.pagination.LinkHeaderPagination",
    "DEFAULT_SCHEMA_CLASS": "rest_framework.schemas.coreapi.AutoSchema",
    "PAGE_SIZE": 10,
    "EXCEPTION_HANDLER": "waldur_core.core.views.exception_handler",
    # Return native `Date` and `Time` objects in `serializer.data`
    "DATETIME_FORMAT": None,
    "DATE_FORMAT": None,
    "TIME_FORMAT": None,
    "ORDERING_PARAM": "o",
}

AUTHENTICATION_BACKENDS = (
    "axes.backends.AxesBackend",
    "django.contrib.auth.backends.ModelBackend",
    "waldur_core.core.authentication.AuthenticationBackend",
)

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

ANONYMOUS_USER_ID = None

CONTEXT_PROCESSORS = (
    "django.template.context_processors.debug",
    "django.template.context_processors.request",
    "django.contrib.auth.context_processors.auth",
    "django.contrib.messages.context_processors.messages",
    "django.template.context_processors.i18n",
    "django.template.context_processors.media",
    "django.template.context_processors.static",
    "django.template.context_processors.tz",
)

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": (os.path.join(BASE_DIR, "src", "waldur_core", "templates"),),
        "OPTIONS": {
            "context_processors": CONTEXT_PROCESSORS,
            "loaders": ADMIN_TEMPLATE_LOADERS
            + (
                "dbtemplates.loader.Loader",
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ),  # noqa: F405
        },
    },
]

ROOT_URLCONF = "waldur_core.server.urls"

AUTH_USER_MODEL = "core.User"

# Session
# https://docs.djangoproject.com/en/2.2/ref/settings/#sessions
SESSION_COOKIE_AGE = 3600
SESSION_SAVE_EVERY_REQUEST = True

WSGI_APPLICATION = "waldur_core.server.wsgi.application"

TIME_ZONE = "UTC"

USE_I18N = True

USE_L10N = True

LOCALE_PATHS = (os.path.join(BASE_DIR, "src", "waldur_core", "locale"),)

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/2.2/howto/static-files/
STATIC_URL = "/static/"

# Celery
CELERY_BROKER_URL = "redis://localhost"
CELERY_RESULT_BACKEND = "redis://localhost"

CELERY_TASK_QUEUES = {
    "tasks": {"exchange": "tasks"},
    "heavy": {"exchange": "heavy"},
    "background": {"exchange": "background"},
}
CELERY_TASK_DEFAULT_QUEUE = "tasks"
CELERY_TASK_ROUTES = ("waldur_core.server.celery.PriorityRouter",)
CELERY_TRACK_STARTED = True
CELERY_SEND_EVENTS = True

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# Regular tasks
CELERY_BEAT_SCHEDULE = {
    "pull-service-properties": {
        "task": "waldur_core.structure.ServicePropertiesListPullTask",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "pull-service-resources": {
        "task": "waldur_core.structure.ServiceResourcesListPullTask",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "check-expired-permissions": {
        "task": "waldur_core.permissions.check_expired_permissions",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "cancel-expired-invitations": {
        "task": "waldur_core.users.cancel_expired_invitations",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "cancel_expired_group_invitations": {
        "task": "waldur_core.users.cancel_expired_group_invitations",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "send-reminder-for-pending-invitations": {
        "task": "waldur_core.users.send_reminder_for_pending_invitations",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "process-pending-project-invitations": {
        "task": "waldur_core.users.process_pending_project_invitations",
        "schedule": timedelta(hours=2),
        "args": (),
    },
    "core-reset-updating-resources": {
        "task": "waldur_core.reset_updating_resources",
        "schedule": timedelta(minutes=10),
        "args": (),
    },
    "structure-set-erred-stuck-resources": {
        "task": "waldur_core.structure.SetErredStuckResources",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "create_customer_permission_reviews": {
        "task": "waldur_core.structure.create_customer_permission_reviews",
        "schedule": timedelta(hours=24),
        "args": (),
    },
    "update-custom-quotas": {
        "task": "waldur_core.quotas.update_custom_quotas",
        "schedule": timedelta(hours=1),
        "args": (),
    },
    "update-standard-quotas": {
        "task": "waldur_core.quotas.update_standard_quotas",
        "schedule": timedelta(hours=24),
        "args": (),
    },
}

globals().update(WaldurConfiguration().dict())

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
}
CONSTANCE_CONFIG = {
    "SITE_NAME": ("Waldur", "Human-friendly name of the Waldur deployment."),
    "SITE_DESCRIPTION": (
        "Your single pane of control for managing projects, teams and resources in a self-service manner.",
        "Description of the Waldur deployment.",
    ),
    "SITE_ADDRESS": ("", "It is used in marketplace order header."),
    "SITE_EMAIL": ("", "It is used in marketplace order header and UI footer."),
    "SITE_PHONE": ("", "It is used in marketplace order header and UI footer."),
    "CURRENCY_NAME": (
        "EUR",
        "It is used in marketplace order details and invoices for currency formatting.",
    ),
    "DOCS_URL": ("", "Renders link to docs in header", "url_field"),
    "SHORT_PAGE_TITLE": ("Waldur", "It is used as prefix for page title."),
    "FULL_PAGE_TITLE": (
        "Waldur | Cloud Service Management",
        "It is used as default page title if it's not specified explicitly.",
    ),
    "BRAND_COLOR": (
        "#3a8500",
        "Hex color definition is used in HomePort landing page for login button.",
        "color_field",
    ),
    "BRAND_LABEL_COLOR": (
        "#000000",
        "Hex color definition is used in HomePort landing page for font color of login button.",
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
    "DISABLE_DARK_THEME": (False, "Toggler for dark theme."),
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
    "ATLASSIAN_USE_OLD_API": (
        False,
        "Toggler for legacy API usage.",
    ),
    "ATLASSIAN_USE_TEENAGE_API": (
        False,
        "Toggler for teenage API usage.",
    ),
    "ATLASSIAN_USE_AUTOMATIC_REQUEST_MAPPING": (
        True,
        "Toggler for automatic request mapping.",
    ),
    "ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS": (
        False,
        "Toggler for mapping between waldur user and service desk agents.",
    ),
    "ATLASSIAN_STRANGE_SETTING": (1, "A constant in the API path, sometimes differs"),
    "ATLASSIAN_API_URL": (
        "http://example.com/",
        "Atlassian API server URL",
        "url_field",
    ),
    "ATLASSIAN_USERNAME": ("USERNAME", "Username for access user"),
    "ATLASSIAN_PASSWORD": ("PASSWORD", "Password for access user", "secret_field"),
    "ATLASSIAN_EMAIL": ("", "Email for access user", "email_field"),
    "ATLASSIAN_TOKEN": ("", "Token for access user", "secret_field"),
    "ATLASSIAN_VERIFY_SSL": (
        False,
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
    "ATLASSIAN_PULL_PRIORITIES": (
        True,
        "Toggler for pulling priorities from backend",
    ),
    "ATLASSIAN_ISSUE_TYPES": (
        "Informational, Service Request, Change Request, Incident",
        "Comma-separated list of enabled issue types. First type is the default one.",
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
    # Zammad settings
    "ZAMMAD_API_URL": (
        "",
        "Zammad API server URL. For example <http://localhost:8080/>",
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
        "SMAX API server URL. For example <http://localhost:8080/>",
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
    # Proposal settings
    "PROPOSAL_REVIEW_DURATION": (7, "Review duration in days."),
    "USER_TABLE_COLUMNS": ("", "Comma-separated list of columns for users table."),
    "AUTO_APPROVE_USER_TOS": (False, "Configure whether a user needs to approve TOS."),
}

CONSTANCE_CONFIG_FIELDSETS = {
    "Branding": (
        "SITE_NAME",
        "SHORT_PAGE_TITLE",
        "FULL_PAGE_TITLE",
        "SITE_DESCRIPTION",
    ),
    "Marketplace": (
        "SITE_ADDRESS",
        "SITE_EMAIL",
        "SITE_PHONE",
        "CURRENCY_NAME",
    ),
    "Notifications": (
        "COMMON_FOOTER_TEXT",
        "COMMON_FOOTER_HTML",
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
        "BRAND_LABEL_COLOR",
        "DISABLE_DARK_THEME",
    ),
    "Images": (
        "SITE_LOGO",
        "SIDEBAR_LOGO",
        "SIDEBAR_LOGO_MOBILE",
        "SIDEBAR_LOGO_DARK",
        "POWERED_BY_LOGO",
        "HERO_IMAGE",
        "LOGIN_LOGO",
        "FAVICON",
        "OFFERING_LOGO_PLACEHOLDER",
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
        "ATLASSIAN_PROJECT_ID",
        "ATLASSIAN_DEFAULT_OFFERING_ISSUE_TYPE",
        "ATLASSIAN_EXCLUDED_ATTACHMENT_TYPES",
        "ATLASSIAN_ISSUE_TYPES",
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
        "ATLASSIAN_CUSTOM_ISSUE_FIELD_MAPPING_ENABLED",
        "ATLASSIAN_SHARED_USERNAME",
        "ATLASSIAN_VERIFY_SSL",
        "ATLASSIAN_USE_OLD_API",
        "ATLASSIAN_USE_TEENAGE_API",
        "ATLASSIAN_USE_AUTOMATIC_REQUEST_MAPPING",
        "ATLASSIAN_MAP_WALDUR_USERS_TO_SERVICEDESK_AGENTS",
        "ATLASSIAN_STRANGE_SETTING",
        "ATLASSIAN_PULL_PRIORITIES",
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
    "Proposal settings": ("PROPOSAL_REVIEW_DURATION",),
    "Table settings": ("USER_TABLE_COLUMNS",),
    "Localization": ("LANGUAGE_CHOICES",),
    "User settings": ("AUTO_APPROVE_USER_TOS",),
}

PUBLIC_CONSTANCE_SETTINGS = (
    # Whitelabeling settings
    "SITE_NAME",
    "SITE_DESCRIPTION",
    "SITE_ADDRESS",
    "SITE_EMAIL",
    "SITE_PHONE",
    "CURRENCY_NAME",
    "DOCS_URL",
    "SHORT_PAGE_TITLE",
    "FULL_PAGE_TITLE",
    "BRAND_COLOR",
    "BRAND_LABEL_COLOR",
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
    "LOGIN_LOGO",
    "FAVICON",
    "OFFERING_LOGO_PLACEHOLDER",
    "COMMON_FOOTER_TEXT",
    "COMMON_FOOTER_HTML",
    "LANGUAGE_CHOICES",
    "DISABLE_DARK_THEME",
    # Support plugin
    "WALDUR_SUPPORT_ENABLED",
    "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE",
    "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE",
    # Proposal
    "PROPOSAL_REVIEW_DURATION",
    # Tables
    "USER_TABLE_COLUMNS",
)

for ext in WaldurExtension.get_extensions():
    INSTALLED_APPS += (ext.django_app(),)

    for name, task in ext.celery_tasks().items():
        if name in CELERY_BEAT_SCHEDULE:
            warnings.warn(
                "Celery beat task %s from Waldur extension %s "
                "is overlapping with primary tasks definition"
                % (name, ext.django_app())
            )
        else:
            CELERY_BEAT_SCHEDULE[name] = task

    for key, val in ext.Settings.__dict__.items():
        if not key.startswith("_"):
            globals()[key] = val

    ext.update_settings(globals())

# Swagger
SWAGGER_SETTINGS = {
    # USE_SESSION_AUTH parameter should be equal to DEBUG parameter.
    # If it is True, LOGIN_URL and LOGOUT_URL must be specified.
    "USE_SESSION_AUTH": False,
    "APIS_SORTER": "alpha",
    "JSON_EDITOR": True,
    "SECURITY_DEFINITIONS": {
        "api_key": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
        },
    },
}

AXES_ONLY_USER_FAILURES = True
AXES_COOLOFF_TIME = timedelta(minutes=10)
AXES_FAILURE_LIMIT = 5

# Django File Storage API
DEFAULT_FILE_STORAGE = "binary_database_files.storage.DatabaseStorage"
DB_FILES_AUTO_EXPORT_DB_TO_FS = False
DATABASE_FILES_URL_METHOD = "URL_METHOD_2"

# Disable excessive xmlschema and django-axes logging
import logging

logging.getLogger("xmlschema").propagate = False
logging.getLogger("axes").propagate = False

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

LANGUAGES = (
    ("en", "English"),
    ("et", "Eesti"),
    ("lt", "Lietuvių"),
    ("lv", "Latviešu"),
    ("ru", "Русский"),
    ("it", "Italiano"),
    ("de", "Deutsch"),
    ("da", "Dansk"),
    ("sv", "Svenska"),
    ("es", "Español"),
    ("fr", "Français"),
    ("nb", "Norsk"),
    ("ar", "العربية"),
    ("cs", "Čeština"),
)
