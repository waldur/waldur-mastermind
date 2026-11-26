"""The fields import_offering copies from an offering document.

Offering templates read them too: a template parameter may fill only what
the import copies.
"""

# Model fields that the offering import copies from the imported data.
OFFERING_IMPORT_FIELDS = (
    "name",
    "description",
    "full_description",
    "vendor_details",
    "getting_started",
    "integration_guide",
    "type",
    "shared",
    "billable",
    "country",
    "latitude",
    "longitude",
    "access_url",
    "paused_reason",
    # Not in the export, so an export moved to another portal never carries
    # it; an offering template fills it from a parameter (the cluster name).
    "backend_id",
)
COMPONENT_IMPORT_FIELDS = (
    "name",
    "description",
    "billing_type",
    "measured_unit",
    "unit_factor",
    "limit_period",
    "limit_amount",
    "limit_increase_only",
    "article_code",
    "backend_id",
    "min_value",
    "max_value",
    "max_available_limit",
    "default_limit",
)
PLAN_IMPORT_FIELDS = (
    "description",
    "unit_price",
    "unit",
    "archived",
    "max_amount",
    "article_code",
    "backend_id",
)
PLAN_COMPONENT_IMPORT_FIELDS = ("amount", "price", "future_price")
TERMS_OF_SERVICE_IMPORT_FIELDS = (
    "terms_of_service",
    "terms_of_service_link",
    "version",
    "is_active",
    "requires_reconsent",
    "grace_period_days",
)
ENDPOINT_IMPORT_FIELDS = ("url",)
