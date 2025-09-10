from typing import NotRequired, TypedDict


class InvoiceResourceLimitPeriodDict(TypedDict):
    """
    Represents a specific time-bound period for a limit-based component.

    When a resource's limit is changed during a billing period, a new period is
    created to allow for accurate prorated billing. A list of these dictionaries
    is stored in the invoice item's `details` field.
    """

    start: str  # Start datetime of the period, serialized in ISO 8601 format.
    end: str  # End datetime of the period, serialized in ISO 8601 format.
    quantity: int  # The resource limit amount (e.g., 10 GB) active during this period.
    billing_periods: int  # The number of full days in the period, used for proration.
    total: str  # Total prorated usage for the period (quantity * billing_periods), serialized as a string.


class InvoiceDetailsDict(TypedDict):
    """
    Structured metadata for an invoice item, providing detailed context.

    This dictionary is stored in the `details` JSON field of an InvoiceItem.
    It contains comprehensive information about the billed resource, its offering,
    plan, components, and any applied promotions, facilitating detailed reporting
    and analytics.
    """

    # --- Core Identification Fields (Always Present) ---
    offering_component_name: (
        str  # Display name of the marketplace offering component (e.g., 'CPU cores').
    )
    offering_component_type: (
        str  # Internal type/key of the offering component (e.g., 'cpu').
    )
    offering_name: str  # Name of the marketplace offering.
    offering_type: str  # Type of the offering, used to identify the plugin handler (e.g., 'Marketplace.Basic').
    offering_uuid: str  # UUID of the marketplace offering.
    plan_component_id: str  # Database ID of the plan component.
    plan_name: str  # Name of the marketplace plan.
    plan_uuid: str  # UUID of the marketplace plan.
    resource_name: str  # Name of the provisioned marketplace resource.
    resource_uuid: str  # UUID of the provisioned marketplace resource.
    service_provider_name: str  # Name of the customer acting as the service provider.
    service_provider_uuid: str  # UUID of the service provider.

    # --- Optional Fields (Conditionally Present) ---
    campaign_uuid: NotRequired[
        str
    ]  # UUID of a promotional campaign if a discount was applied.
    resource_limit_periods: NotRequired[
        list[InvoiceResourceLimitPeriodDict]
    ]  # For limit-based items, a list of periods detailing limit changes.
    unit_price: NotRequired[
        float
    ]  # The original unit price before a campaign discount was applied.
