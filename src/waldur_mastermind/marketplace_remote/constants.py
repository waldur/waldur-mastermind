from waldur_core.core.client import (
    MAX_FETCH_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_RESPONSE_VALUES,
    RemoteCallLimits,
)

OFFERING_FIELDS = (
    "name",
    "description",
    "full_description",
    "privacy_policy_link",
    "getting_started",
    "integration_guide",
    "country",
    "options",
    "resource_options",
    "plugin_options",
    "access_url",
)

# Plugin options that configure the consumer side of an offering — what this
# Waldur's own users may ask for — rather than the provider's integration. They
# keep their local value when the remote offering is pulled; a key that was
# never set locally still follows the remote.
LOCAL_PLUGIN_OPTIONS = (
    "enable_resource_end_date_change_requests",
    "enable_resource_limit_change_requests",
)

OFFERING_COMPONENT_FIELDS = (
    "name",
    "type",
    "description",
    "article_code",
    "measured_unit",
    "billing_type",
    "min_value",
    "max_value",
    "is_boolean",
    "default_limit",
    "limit_period",
    "limit_amount",
    "limit_decimal_places",
)

PLAN_FIELDS = (
    "name",
    "description",
    "article_code",
    "archived",
)

RESOURCE_FIELDS = ("report", "attributes", "options")


# An offering's image, thumbnail and screenshots, fetched in one go when it is
# imported or pulled: the screenshot list plus two files per screenshot, so
# about 25 screenshots, and up to five full-size files.
OFFERING_FILES_LIMITS = RemoteCallLimits(
    max_requests=60,
    timeout=300,
    max_bytes=5 * MAX_FETCH_BYTES,
    max_values=MAX_RESPONSE_VALUES,
)
# Importing an offering in the request that asks for it, from resolving the
# remote's host to the last file: one budget within gunicorn's 30-second
# worker timeout, inside which the steps take their own.
OFFERING_IMPORT_LIMITS = RemoteCallLimits(
    max_requests=OFFERING_FILES_LIMITS.max_requests + 1,
    timeout=25,
    max_bytes=OFFERING_FILES_LIMITS.max_bytes + MAX_RESPONSE_BYTES,
    max_values=2 * MAX_RESPONSE_VALUES,
)
# Pushing a resource's project team: up to four requests to set up, and up to
# three for each member (look the user up, read the role, add or update it).
# A team of 1000 fits; fifteen minutes stays inside the Celery soft time limit
# of twenty.
TEAM_SYNC_LIMITS = RemoteCallLimits(
    max_requests=4 + 3 * 1000,
    timeout=900,
    max_bytes=50 * 1024 * 1024,
    max_values=2_000_000,
)
# A resource's usages since it was created, each of the two lists: a resource
# billed monthly for years on a few components has thousands of records, tens
# of pages; a busy one with per-user usages can reach hundreds. Two calls of
# eight minutes fit the Celery soft time limit of twenty. Six million values
# is about 300 MB parsed.
USAGE_HISTORY_LIMITS = RemoteCallLimits(
    max_requests=3000,
    timeout=480,
    max_bytes=100 * 1024 * 1024,
    max_values=6_000_000,
)
# Every project of a remote Waldur, uuid and backend_id only (a few values
# each): a hub with 100,000 projects is 1000 pages.
REMOTE_PROJECTS_LIMITS = RemoteCallLimits(
    max_requests=2000,
    timeout=300,
    max_bytes=100 * 1024 * 1024,
    max_values=2_000_000,
)
