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
