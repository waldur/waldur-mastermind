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


class ProjectSection(FeatureSection):
    class Meta:
        key = "project"
        description = "Project workspace"

    estimated_cost = Feature("Render estimated cost column in projects list.")

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

    import_resources = Feature(
        "Allow to import resources from service provider to project."
    )

    conceal_prices = Feature("Do not render prices in shopping cart and order details.")

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

    pricelist = Feature(
        "Render marketplace plan components pricelist in support workspace."
    )

    users = Feature("Render list of users in support workspace.")

    shared_providers = Feature(
        "Render overview of shared marketplace service providers in support workspace."
    )

    vm_type_overview = Feature("Enable VM type overview in support workspace.")

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


class OpenstackSection(FeatureSection):
    class Meta:
        key = "openstack"
        description = "OpenStack resources provisioning"

    hide_volume_type_selector = Feature(
        "Allow to hide OpenStack volume type selector when instance or volume is provisioned."
    )


class TelemetrySection(FeatureSection):
    class Meta:
        key = "telemetry"
        description = "Telemetry settings"

    send_metrics = Feature("Send telemetry metrics.")
