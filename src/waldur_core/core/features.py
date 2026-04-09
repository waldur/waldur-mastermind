from dataclasses import dataclass


@dataclass
class Feature:
    description: str


FEATURES = []


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
                        {"key": key, "description": feature.description}
                    )
        return type.__new__(self, name, bases, attrs)


class FeatureSection(metaclass=FeatureSectionMetaclass):
    pass


class CustomerSection(FeatureSection):
    class Meta:
        key = "customer"
        description = "Organization workspace"

    show_domain = Feature("Allows to hide domain field in organization detail.")

    payments_for_staff_only = Feature(
        "Make payments menu visible for staff users only."
    )
    show_permission_reviews = Feature(
        "Allows to show permission reviews tab and popups for organisations."
    )

    show_banking_data = Feature("Display banking related data under customer profile.")
    show_onboarding = Feature("Enable onboarding functionality.")
    show_project_digest = Feature(
        "Enable display of project digest configuration in organization settings."
    )


class ProjectSection(FeatureSection):
    class Meta:
        key = "project"
        description = "Project workspace"

    estimated_cost = Feature("Render estimated cost column in projects list.")

    oecd_fos_2007_code = Feature("Enable OECD code.")

    show_industry_flag = Feature("Show industry flag.")

    show_description_in_create_dialog = Feature(
        "Show description field in project create dialog."
    )

    show_type_in_create_dialog = Feature("Show type field in project create dialog.")

    show_start_date_in_create_dialog = Feature(
        "Show start date field in project create dialog."
    )

    show_end_date_in_create_dialog = Feature(
        "Show end date field in project create dialog."
    )

    show_image_in_create_dialog = Feature("Show image field in project create dialog.")

    show_credit_in_create_dialog = Feature(
        "Show credit field in project create dialog."
    )

    mandatory_start_date = Feature("Make the project start date mandatory.")

    show_permission_reviews = Feature(
        "Allows to show permission reviews tab and popups for projects."
    )

    show_kind_in_create_dialog = Feature("Show kind field in project create dialog.")


class UserSection(FeatureSection):
    class Meta:
        key = "user"
        description = "User workspace"

    preferred_language = Feature("Render preferred language column in users list.")

    ssh_keys = Feature("Enable SSH keys management in user workspace.")

    notifications = Feature(
        "Enable email and webhook notifications management in user workspace."
    )

    disable_user_termination = Feature("Disable user termination in user workspace.")

    show_slug = Feature("Enable display of slug field in user summary.")

    show_username = Feature("Enable display of username field in user tables.")

    pending_user_actions = Feature("Show pending user actions.")

    show_data_access = Feature(
        "Enable Data Access tab showing who can access user profile data."
    )

    show_identity_bridge = Feature(
        "Show identity bridge information in user profiles and admin views."
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


class MarketplaceSection(FeatureSection):
    class Meta:
        key = "marketplace"
        description = "Marketplace offerings and resources"

    import_resources = Feature(
        "Allow to import resources from service provider to project."
    )

    conceal_prices = Feature("Do not render prices in order details.")

    show_experimental_ui_components = Feature(
        "Enabled display of experimental or mocked components in marketplace."
    )

    show_call_management_functionality = Feature(
        "Enabled display of call management functionality."
    )

    lexis_links = Feature("Enabled LEXIS link integrations for offerings.")

    catalogue_only = Feature("Allow marketplace to function as a catalogue only.")

    conceal_offering_pricing_tab_in_public_view = Feature(
        "Conceal offering pricing tab in the offering's public view."
    )

    call_only = Feature("Allow marketplace to serve only as aggregator of call info.")

    show_resource_end_date = Feature(
        "Show resource end date as a non optional column in resources list."
    )

    allow_display_of_images_in_markdown = Feature(
        "Allow display of images in markdown format."
    )
    display_user_tos = Feature("Enable display of user terms of service in UI.")

    display_software_catalog = Feature("Enable display of software catalog in UI.")

    display_offering_partitions = Feature(
        "Enable display of offering partitions in UI."
    )

    conceal_resource_metadata = Feature(
        "Conceal resource metadata from non-staff users in resource detail view."
    )

    hide_marketplace_from_end_users = Feature(
        "Hide marketplace functionality from end users but allow staff access."
    )
    hide_add_resource_button_from_end_users = Feature(
        "Hide add resource button from end users but allow staff/support access."
    )
    hide_organization_information_from_project_members = Feature(
        "Hide organization information from project-level users. Organization owners, managers, and staff retain full access."
    )
    conceal_audit_log_from_end_users = Feature(
        "Hide audit log tab from non-staff and non-support users."
    )

    conceal_pending_provider_orders = Feature(
        "Hide pending provider orders section from the pending confirmations drawer."
    )

    conceal_pending_consumer_orders = Feature(
        "Hide pending consumer orders section from the pending confirmations drawer."
    )


class SupportSection(FeatureSection):
    class Meta:
        key = "support"
        description = "Support workspace"

    pricelist = Feature(
        "Render marketplace plan components pricelist in support workspace."
    )

    vm_type_overview = Feature("Enable VM type overview in support workspace.")

    conceal_change_request = Feature(
        'Conceal "Change request" from a selection of issue types for non-staff/non-support users.'
    )

    enable_llm_assistant = Feature("Enable AI Assistant")


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
    show_service_accounts = Feature("Show service accounts of the scopes.")
    show_course_accounts = Feature("Show course accounts of the scopes.")


class RancherSection(FeatureSection):
    class Meta:
        key = "rancher"
        description = "Rancher resources provisioning"

    volume_mount_point = Feature(
        "Allow to select mount point for data volume when Rancher cluster is provisioned."
    )

    apps = Feature("Render Rancher apps as a separate tab in resource details page.")


class SlurmSection(FeatureSection):
    class Meta:
        key = "slurm"
        description = "SLURM resources provisioning"

    jobs = Feature(
        "Render list of SLURM jobs as a separate tab in allocation details page."
    )


class OpenstackSection(FeatureSection):
    class Meta:
        key = "openstack"
        description = "OpenStack resources provisioning"

    hide_volume_type_selector = Feature(
        "Allow to hide OpenStack volume type selector when instance or volume is provisioned."
    )
    show_migrations = Feature("Show OpenStack tenant migrations action and tab")


class WaldurDeploymentSection(FeatureSection):
    class Meta:
        key = "deployment"
        description = "Waldur deployment settings"

    send_metrics = Feature("Send telemetry metrics.")
    enable_cookie_notice = Feature("Enable cookie notice in marketplace.")
    enable_disclaimer_area = Feature("Enable disclaimer area below the footer.")


class ResellerSection(FeatureSection):
    class Meta:
        key = "reseller"
        description = "Reseller integrations"

    arrow = Feature("Enable Arrow integration menu in administration.")
