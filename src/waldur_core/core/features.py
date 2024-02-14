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

    category_resources_list = Feature(
        "Render component usage charts in organization dashboard."
    )

    project_requests = Feature(
        "Render list of project creation requests in organization dashboard."
    )

    resource_requests = Feature(
        "Render list of resource creation requests in organization dashboard."
    )

    show_subnets = Feature(
        "Render list of subnets from where connection to "
        "self-service is allowed in organization details dialog."
    )

    show_domain = Feature("Allows to hide domain field in organization detail.")

    billing = Feature("Render billing menu in organization sidebar.")

    team = Feature("Enable team management in organization workspace.")

    events = Feature("Enable audit log in organization workspace.")

    hide_organization_billing_step = Feature(
        "Hide billing step in organization creation wizard."
    )

    payments_for_staff_only = Feature(
        "Make payments menu visible for staff users only."
    )


class ProjectSection(FeatureSection):
    class Meta:
        key = "project"
        description = "Project workspace"

    team = Feature("Enable team management in project workspace.")

    estimated_cost = Feature("Render estimated cost column in projects list.")

    events = Feature("Enable audit log in project workspace.")

    oecd_fos_2007_code = Feature("Enable OECD code.")

    show_industry_flag = Feature("Show industry flag.")


class UserSection(FeatureSection):
    class Meta:
        key = "user"
        description = "User workspace"

    preferred_language = Feature("Render preferred language column in users list.")

    competence = Feature("Render competence column in users list.")

    ssh_keys = Feature("Enable SSH keys management in user workspace.")

    notifications = Feature(
        "Enable email and webhook notifications management in user workspace."
    )


class MarketplaceSection(FeatureSection):
    class Meta:
        key = "marketplace"
        description = "Marketplace offerings and resources"

    offering_document = Feature("Allow to attach document to marketplace offering.")

    flows = Feature(
        "Allow to submit organization, project and resource creation requests simultaneously."
    )

    private_offerings = Feature(
        "Render list of private marketplace service providers in organization workspace."
    )

    import_resources = Feature(
        "Allow to import resources from service provider to project."
    )

    conceal_prices = Feature("Do not render prices in shopping cart and order details.")

    terms_of_service = Feature("Render terms of service when offering is ordered.")

    review = Feature("Allow to write a review for marketplace offering.")

    show_experimental_ui_components = Feature(
        "Enabled display of experimental or mocked components in marketplace."
    )

    show_call_management_functionality = Feature(
        "Enabled display of call management functionality."
    )
    lexis_links = Feature("Enabled LEXIS link integrations for offerings.")


class SupportSection(FeatureSection):
    class Meta:
        key = "support"
        description = "Support workspace"

    activity_stream = Feature("Render list of recent comments in support dashboard.")

    customers_list = Feature("Render list of organizations in support workspace.")

    pricelist = Feature(
        "Render marketplace plan components pricelist in support workspace."
    )

    customers_requests = Feature(
        "Render list of organization creation requests in support workspace."
    )

    users = Feature("Render list of users in support workspace.")

    flowmap = Feature("Render service usage as a flowmap chart in support workspace.")

    heatmap = Feature("Render service usage as a heatmap chart in support workspace.")

    sankey_diagram = Feature(
        "Render service usage as a sankey chart in support workspace."
    )

    resources_treemap = Feature(
        "Render resource usage as a treemap chart in support workspace."
    )

    shared_providers = Feature(
        "Render overview of shared marketplace service providers in support workspace."
    )

    resource_usage = Feature(
        "Enable resource usage overview charts in support workspace."
    )

    vm_type_overview = Feature("Enable VM type overview in support workspace.")

    offering_comments = Feature(
        "Render comments tab in request-based item details page."
    )

    conceal_change_request = Feature(
        'Conceal "Change request" from a selection of issue types for non-staff/non-support users.'
    )


class InvitationsSection(FeatureSection):
    class Meta:
        key = "invitations"
        description = "Invitations management"

    conceal_civil_number = Feature(
        "Conceal civil number in invitation creation dialog."
    )

    create_missing_user = Feature(
        "Allow to create FreeIPA user using details "
        "specified in invitation if user does not exist yet."
    )

    disable_multiple_roles = Feature(
        "Do not allow user to grant multiple roles in the "
        "same project or organization using invitation."
    )

    show_tax_number = Feature("Show tax number field in invitation creation form.")

    tax_number_required = Feature(
        "Make tax number field mandatory in invitation creation form."
    )

    civil_number_required = Feature(
        "Make civil number field mandatory in invitation creation form."
    )

    require_user_details = Feature(
        'Render "Show user details" button in invitation creation form.'
    )


class InvoiceSection(FeatureSection):
    class Meta:
        key = "invoice"
        description = "Invoice management"

    events = Feature("Render list of events related to invoice item in modal dialog.")


class RancherSection(FeatureSection):
    class Meta:
        key = "rancher"
        description = "Rancher resources provisioning"

    volume_mount_point = Feature(
        "Allow to select mount point for data volume when Rancher cluster is provisioned."
    )


class SlurmSection(FeatureSection):
    class Meta:
        key = "slurm"
        description = "SLURM resources provisioning"

    jobs = Feature(
        "Render list of SLURM jobs as a separate tab in allocation details page."
    )
