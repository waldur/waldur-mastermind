from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.apps import apps

# A deployment-scoped flag has one value for the whole portal. A user-scoped
# flag may also be turned on for selected users by FeatureRule (waldur_core.users):
# only a flag whose effect is the signed-in user's own view qualifies. Flags
# that are shown to anonymous visitors, change what the whole deployment is, or
# change what other people see stay deployment-scoped.
SCOPE_DEPLOYMENT = "deployment"
SCOPE_USER = "user"
SCOPES = (SCOPE_DEPLOYMENT, SCOPE_USER)


@dataclass
class Feature:
    description: str
    # Value used while no core.Feature row exists, i.e. until an admin or
    # load_features sets the flag explicitly.
    default: bool = False
    scope: str = SCOPE_DEPLOYMENT

    def __post_init__(self):
        if self.scope not in SCOPES:
            raise ValueError(f"Unknown feature scope {self.scope!r}.")


FEATURES = []

# Dotted keys of the features whose default is on.
FEATURE_DEFAULTS: dict[str, bool] = {}

# Dotted keys of the features that FeatureRule may turn on for selected users.
USER_SCOPED_FEATURES: set[str] = set()


class FeatureSectionMetaclass(type):
    def __new__(self, name, bases, attrs):
        if "Meta" in attrs:
            section = {
                "key": attrs["Meta"].key,
                "description": attrs["Meta"].description,
                "items": [],
            }
            FEATURES.append(section)
            for key, feature in attrs.items():
                if isinstance(feature, Feature):
                    section["items"].append(
                        {
                            "key": key,
                            "description": feature.description,
                            "scope": feature.scope,
                        }
                    )
                    dotted = f"{section['key']}.{key}"
                    if feature.default:
                        FEATURE_DEFAULTS[dotted] = True
                    if feature.scope == SCOPE_USER:
                        USER_SCOPED_FEATURES.add(dotted)
        return type.__new__(self, name, bases, attrs)


class FeatureSection(metaclass=FeatureSectionMetaclass):
    pass


class CustomerSection(FeatureSection):
    class Meta:
        key = "customer"
        description = "Organization workspace"

    show_domain = Feature(
        "Allows to hide domain field in organization detail.", scope=SCOPE_USER
    )

    payments_for_staff_only = Feature(
        "Make payments menu visible for staff users only."
    )
    show_invoices_with_vat = Feature(
        "Show costs and prices with VAT included and present monthly invoices "
        "instead of accounting records. When off, costs are shown without VAT."
    )
    show_permission_reviews = Feature(
        "Allows to show permission reviews tab and popups for organizations."
    )

    show_banking_data = Feature(
        "Display banking related data under customer profile.", scope=SCOPE_USER
    )
    show_onboarding = Feature("Enable onboarding functionality.")
    show_project_digest = Feature(
        "Enable display of project digest configuration in organization settings.",
        scope=SCOPE_USER,
    )
    show_organisation_reporting = Feature(
        "Show the Reporting menu with organization-scoped reports to users with "
        "the CUSTOMER.VIEW_REPORTING permission on the organization.",
        scope=SCOPE_USER,
    )
    show_openportal_remote_projects = Feature(
        "Show the Remote Projects pages of a portal that sends projects to other "
        "portals over OpenPortal. Needs the OpenPortal plugin to be enabled.",
        default=True,
    )


class ProjectSection(FeatureSection):
    class Meta:
        key = "project"
        description = "Project workspace"

    estimated_cost = Feature(
        "Render estimated cost column in projects list.", scope=SCOPE_USER
    )

    oecd_fos_2007_code = Feature("Enable OECD code.")

    science_domain = Feature("Enable science domain/sub-domain selection for projects.")

    show_industry_flag = Feature("Show industry flag.")

    show_description_in_create_dialog = Feature(
        "Show description field in project create dialog.", scope=SCOPE_USER
    )

    show_type_in_create_dialog = Feature(
        "Show type field in project create dialog.", scope=SCOPE_USER
    )

    show_start_date_in_create_dialog = Feature(
        "Show start date field in project create dialog.", scope=SCOPE_USER
    )

    show_end_date_in_create_dialog = Feature(
        "Show end date field in project create dialog.", scope=SCOPE_USER
    )

    show_image_in_create_dialog = Feature(
        "Show image field in project create dialog.", scope=SCOPE_USER
    )

    show_credit_in_create_dialog = Feature(
        "Show credit field in project create dialog.", scope=SCOPE_USER
    )

    mandatory_start_date = Feature("Make the project start date mandatory.")

    show_permission_reviews = Feature(
        "Allows to show permission reviews tab and popups for projects."
    )

    show_kind_in_create_dialog = Feature(
        "Show kind field in project create dialog.", scope=SCOPE_USER
    )

    show_matrix_chat = Feature(
        "Render the project Matrix chat UI. Backend access is gated "
        "separately on the MATRIX_ENABLED setting.",
        scope=SCOPE_USER,
    )

    show_custom_metrics = Feature(
        "Show custom metrics: the project Metrics tab and the offering metrics "
        "a provider adopts.",
        scope=SCOPE_USER,
    )

    enforce_allowed_domains = Feature(
        "Enforce the rules of an OpenPortal award on local team changes. Despite "
        "its name, this flag gates both the award's membership and role lock and "
        "its allowed email domains: adding, removing or re-roling members and "
        "inviting users is refused if the award does not permit it."
    )


class UserSection(FeatureSection):
    class Meta:
        key = "user"
        description = "User workspace"

    preferred_language = Feature(
        "Render preferred language column in users list.", scope=SCOPE_USER
    )

    ssh_keys = Feature(
        "Enable SSH keys management in user workspace.", scope=SCOPE_USER
    )

    notifications = Feature(
        "Enable email and webhook notifications management in user workspace.",
        scope=SCOPE_USER,
    )

    disable_user_termination = Feature("Disable user termination in user workspace.")

    show_slug = Feature(
        "Enable display of slug field in user summary.", scope=SCOPE_USER
    )

    show_username = Feature(
        "Enable display of username field in user tables.", scope=SCOPE_USER
    )

    pending_user_actions = Feature("Show pending user actions.", scope=SCOPE_USER)

    show_data_access = Feature(
        "Enable Data Access tab showing who can access user profile data.",
        scope=SCOPE_USER,
    )

    show_identity_bridge = Feature(
        "Show identity bridge information in user profiles and admin views.",
        scope=SCOPE_USER,
    )

    conceal_api_token = Feature(
        "Hide API token management tab from non-staff and non-support users."
    )

    conceal_permission_requests = Feature(
        "Hide permission requests tab from non-staff and non-support users."
    )

    conceal_remote_accounts = Feature(
        "Hide remote accounts tab from non-staff and non-support users."
    )

    show_micro_apps = Feature(
        "List the deployment's micro-apps in the quick shortcuts menu, "
        "to the users each app is meant for."
    )


class SramSection(FeatureSection):
    class Meta:
        key = "sram"
        description = "SRAM integration"

    integration = Feature(
        "Render the SRAM integration administration page and SRAM markers in team "
        "lists. Backend access is gated separately on the SRAM_INTEGRATION_ENABLED "
        "setting."
    )


class MarketplaceSection(FeatureSection):
    class Meta:
        key = "marketplace"
        description = "Marketplace offerings and resources"

    import_resources = Feature(
        "Allow to import resources from service provider to project."
    )

    conceal_prices = Feature("Do not render prices in order details.")

    show_experimental_ui_components = Feature(
        "Enabled display of experimental or mocked components in marketplace.",
        scope=SCOPE_USER,
    )

    realtime_updates = Feature(
        "Enable push-driven UI updates over the event-consumer WebSocket "
        "(experimental). When disabled, the UI relies on polling and manual "
        "refresh as before. Requires RabbitMQ web-STOMP to be reachable at "
        "/rmqws-stomp on the API host.",
        scope=SCOPE_USER,
    )

    show_call_management_functionality = Feature(
        "Show call management: the call-managing organization's workspace, "
        "providers' requests for offerings, and proposal reports. Independent "
        "of SERVICE_ACCESS_MODE, which governs how applicants reach services."
    )

    lexis_links = Feature(
        "Enabled LEXIS link integrations for offerings.", scope=SCOPE_USER
    )

    catalogue_only = Feature(
        "Run the marketplace as a catalog: offerings can be browsed but not "
        "ordered, order lists and pricing are hidden, and anonymous visitors "
        "land on the marketplace instead of the login page."
    )

    conceal_offering_pricing_tab_in_public_view = Feature(
        "Conceal offering pricing tab in the offering's public view."
    )

    call_only = Feature(
        "List calls that are run outside Waldur: each call links to an external "
        "URL where applications are made, and rounds, proposals and reviews "
        "are hidden."
    )

    show_resource_end_date = Feature(
        "Show resource end date as a non optional column in resources list.",
        scope=SCOPE_USER,
    )

    show_posix_id_pools = Feature(
        "Render POSIX ID pool management UI for service providers.", scope=SCOPE_USER
    )

    show_provider_accounts = Feature(
        "Render service provider account management UI: provider accounts, "
        "username conflicts, the shared GLAuth directory and provider account "
        "settings.",
        scope=SCOPE_USER,
    )

    allow_display_of_images_in_markdown = Feature(
        "Allow display of images in markdown format.", scope=SCOPE_USER
    )
    display_user_tos = Feature("Enable display of user terms of service in UI.")

    display_software_catalog = Feature(
        "Enable display of software catalog in UI.", scope=SCOPE_USER
    )

    show_openstack_duplicate_offerings = Feature(
        "Show the staff diagnostics page listing tenants with duplicate "
        "per-tenant OpenStack offerings.",
        scope=SCOPE_USER,
    )

    display_offering_partitions = Feature(
        "Enable display of offering partitions in UI.", scope=SCOPE_USER
    )
    show_managed_projects = Feature(
        "Show the Managed projects and Project templates pages of a portal that "
        "receives projects from other portals over OpenPortal. Needs the "
        "OpenPortal plugin to be enabled.",
        default=True,
    )

    conceal_resource_metadata = Feature(
        "Conceal resource metadata from non-staff users in resource detail view."
    )

    hide_marketplace_from_end_users = Feature(
        "Hide marketplace functionality from end users but allow staff access."
    )
    hide_organization_information_from_project_members = Feature(
        "Hide organization information from project-level users. Organization owners, managers, and staff retain full access."
    )
    conceal_audit_log_from_end_users = Feature(
        "Hide audit log tab from non-staff and non-support users."
    )

    conceal_pending_provider_orders = Feature(
        "Hide pending provider orders section from the pending confirmations drawer.",
        scope=SCOPE_USER,
    )

    conceal_pending_consumer_orders = Feature(
        "Hide pending consumer orders section from the pending confirmations drawer.",
        scope=SCOPE_USER,
    )


class DashboardSection(FeatureSection):
    class Meta:
        key = "dashboard"
        description = "Project and organization dashboards"

    usage_per_offering_bars = Feature(
        "Show usage against the remaining cap as one stacked bar per offering.",
        scope=SCOPE_USER,
    )

    usage_treemap = Feature(
        "Show usage as a treemap of offering, billing type and component.",
        scope=SCOPE_USER,
    )

    usage_limit_horizon = Feature(
        "Show a saturation bar per offering component against a timeline, "
        "with the current date and the next limit reset marked.",
        scope=SCOPE_USER,
    )

    usage_period_over_period = Feature(
        "Show usage for the current period overlaid on the previous one, "
        "per offering component.",
        scope=SCOPE_USER,
    )

    usage_include_terminated = Feature(
        "Let usage charts include terminated resources: an organization "
        "dashboard switch, and a removed project's own usage. While off, the "
        "usage endpoints ignore include_terminated.",
        scope=SCOPE_USER,
    )

    spend_forecast = Feature(
        "Show credit burn-down with a projected exhaustion date on the "
        "project dashboard.",
        scope=SCOPE_USER,
    )


class SupportSection(FeatureSection):
    class Meta:
        key = "support"
        description = "Support workspace"

    pricelist = Feature(
        "Render marketplace plan components pricelist in support workspace.",
        scope=SCOPE_USER,
    )

    vm_type_overview = Feature(
        "Enable VM type overview in support workspace.", scope=SCOPE_USER
    )

    conceal_change_request = Feature(
        'Conceal "Change request" from a selection of issue types for non-staff/non-support users.'
    )

    enable_llm_assistant = Feature("Enable AI Assistant", scope=SCOPE_USER)


class InvitationsSection(FeatureSection):
    class Meta:
        key = "invitations"
        description = "Invitations management"

    conceal_civil_number = Feature(
        "Conceal civil number in invitation creation dialog."
    )
    civil_number_required = Feature(
        "Make civil number field mandatory in invitation creation form."
    )
    show_service_accounts = Feature(
        "Show service accounts of the scopes.", scope=SCOPE_USER
    )
    show_course_accounts = Feature(
        "Show course accounts of the scopes.", scope=SCOPE_USER
    )


class RancherSection(FeatureSection):
    class Meta:
        key = "rancher"
        description = "Rancher resources provisioning"

    volume_mount_point = Feature(
        "Allow to select mount point for data volume when Rancher cluster is provisioned.",
        scope=SCOPE_USER,
    )

    apps = Feature(
        "Render Rancher apps as a separate tab in resource details page.",
        scope=SCOPE_USER,
    )


class OpenstackSection(FeatureSection):
    class Meta:
        key = "openstack"
        description = "OpenStack resources provisioning"

    hide_volume_type_selector = Feature(
        "Allow to hide OpenStack volume type selector when instance or volume is provisioned.",
        scope=SCOPE_USER,
    )
    show_migrations = Feature(
        "Show OpenStack tenant migrations action and tab", scope=SCOPE_USER
    )


class WaldurDeploymentSection(FeatureSection):
    class Meta:
        key = "deployment"
        description = "Waldur deployment settings"

    send_metrics = Feature("Send telemetry metrics.", default=True)
    enable_cookie_notice = Feature("Enable cookie notice in marketplace.")
    enable_disclaimer_area = Feature("Enable disclaimer area below the footer.")


class ResellerSection(FeatureSection):
    class Meta:
        key = "reseller"
        description = "Reseller integrations"

    arrow = Feature(
        "Enable Arrow integration menu in administration.", scope=SCOPE_USER
    )
    affiliates = Feature(
        "Show affiliate program menus and pages. Backend enforcement is "
        "controlled separately by the AFFILIATES_ENABLED setting."
    )


def _feature_model():
    # Looked up through the app registry: this module is imported while
    # settings load (openapi_settings reads SCOPES), before models exist.
    return apps.get_model("core", "Feature")


def all_feature_keys() -> list[str]:
    """The dotted key of every declared feature, as "<section>.<feature>"."""
    return [
        f"{section['key']}.{feature['key']}"
        for section in FEATURES
        for feature in section["items"]
    ]


def global_values() -> dict[str, dict[str, bool]]:
    """Every declared flag's deployment-wide value, as ``section -> key -> on``."""
    FeatureModel = _feature_model()
    stored = dict(FeatureModel.objects.values_list("key", "value"))
    return {
        section["key"]: {
            feature["key"]: stored.get(
                f"{section['key']}.{feature['key']}",
                FEATURE_DEFAULTS.get(f"{section['key']}.{feature['key']}", False),
            )
            for feature in section["items"]
        }
        for section in FEATURES
    }


def is_user_scoped(key: str) -> bool:
    """Whether FeatureRule may turn this declared flag on for selected users."""
    return key in USER_SCOPED_FEATURES


# Returns the keys that active FeatureRules turn on for a user. Installed by
# waldur_core.users at startup, which owns the rules: this module is imported
# by models that waldur_core.users itself depends on.
_targeted_keys_resolver: Callable[[Any], frozenset[str]] | None = None


def set_targeted_keys_resolver(resolver: Callable[[Any], frozenset[str]]) -> None:
    global _targeted_keys_resolver
    _targeted_keys_resolver = resolver


def targeted_keys(user) -> frozenset[str]:
    """Keys that active FeatureRules turn on for ``user``; none for anonymous."""
    if (
        _targeted_keys_resolver is None
        or user is None
        or not getattr(user, "is_authenticated", False)
    ):
        return frozenset()
    return _targeted_keys_resolver(user)


def is_enabled(key: str, user=None) -> bool:
    """Current value of a feature flag, falling back to its declared default.

    With a ``user``, a user-scoped flag that is off globally is also on when an
    active FeatureRule matches that user. Without one (Celery tasks, anonymous
    requests) only the global value counts.
    """
    FeatureModel = _feature_model()
    value = FeatureModel.objects.filter(key=key).values_list("value", flat=True).first()
    enabled = FEATURE_DEFAULTS.get(key, False) if value is None else value
    if enabled or user is None or not is_user_scoped(key):
        return enabled
    return key in targeted_keys(user)
