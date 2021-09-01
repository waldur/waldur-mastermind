from dataclasses import dataclass


@dataclass
class Feature:
    description: str


FEATURES = []


class FeatureSectionMetaclass(type):
    def __new__(self, name, bases, attrs):
        if 'Meta' in attrs:
            section = {
                'key': attrs['Meta'].key.upper(),
                'description': attrs['Meta'].description,
                'items': [],
            }
            FEATURES.append(section)
            for key, feature in attrs.items():
                if isinstance(feature, Feature):
                    section['items'].append(
                        {'key': key.upper(), 'description': feature.description}
                    )
        return type.__new__(self, name, bases, attrs)


class FeatureSection(metaclass=FeatureSectionMetaclass):
    pass


class CustomerSection(FeatureSection):
    class Meta:
        key = 'customer'
        description = 'Organization workspace'

    category_resources_list = Feature(
        'Render component usage charts in organization dashboard.'
    )

    project_requests = Feature(
        'Render list of project creation requests in organization dashboard.'
    )

    resource_requests = Feature(
        'Render list of resource creation requests in organization dashboard.'
    )

    show_subnets = Feature(
        'Render list of subnets from where connection to '
        'self-service is allowed in organization details dialog.'
    )

    show_domain = Feature('Allows to hide domain field in organization detail.')


class ProjectSection(FeatureSection):
    class Meta:
        key = 'project'
        description = 'Project workspace'

    member_role = Feature('Allow to grant user a project member role.')

    team = Feature('Enable team management in project workspace.')

    estimated_cost = Feature('Render estimated cost column in projects list.')


class UserSection(FeatureSection):
    class Meta:
        key = 'user'
        description = 'User workspace'

    preferred_language = Feature('Render preferred language column in users list.')

    competence = Feature('Render competence column in users list.')

    ssh_keys = Feature('Enable SSH keys management in user workspace.')

    notifications = Feature(
        'Enable email and webhook notifications management in user workspace.'
    )


class MarktplaceSection(FeatureSection):
    class Meta:
        key = 'marketplace'
        description = 'Marketplace offerings and resources'

    offering_document = Feature('Allow to attach document to marketplace offering.')

    flows = Feature(
        'Allow to submit organization, project and resource creation requests simultaneously.'
    )

    private_offerings = Feature(
        'Render list of private marketplace service providers in organization workspace.'
    )


class SupportSection(FeatureSection):
    class Meta:
        key = 'support'
        description = 'Support workspace'

    activity_stream = Feature('Render list of recent comments in support dashboard.')

    customers_list = Feature('Render list of organizations in support workspace.')

    pricelist = Feature(
        'Render marketplace plan components pricelist in support workspace.'
    )

    customers_requests = Feature(
        'Render list of organization creation requests in support workspace.'
    )

    users = Feature('Render list of users in support workspace.')

    flowmap = Feature('Render service usage as a flowmap chart in support workspace.')

    heatmap = Feature('Render service usage as a heatmap chart in support workspace.')

    sankey_diagram = Feature(
        'Render service usage as a sankey chart in support workspace.'
    )

    resources_treemap = Feature(
        'Render resource usage as a treemap chart in support workspace.'
    )

    shared_providers = Feature(
        'Render overview of shared marketplace service providers in support workspace.'
    )

    resource_usage = Feature(
        'Enable resource usage overview charts in support workspace.'
    )

    vm_type_overview = Feature('Enable VM type overview in support workspace.')


class InvitationsSection(FeatureSection):
    class Meta:
        key = 'invitations'
        description = 'Invitations management'

    conceal_civil_number = Feature(
        'Conceal civil number in invitation creation dialog.'
    )

    create_missing_user = Feature(
        'Allow to create FreeIPA user using details '
        'specified in invitation if user does not exist yet.'
    )

    disable_multiple_roles = Feature(
        'Do not allow user to grant multiple roles in the '
        'same project or organization using invitation.'
    )


class InvoiceSection(FeatureSection):
    class Meta:
        key = 'invoice'
        description = 'Invoice management'

    events = Feature('Render list of events related to invoice item in modal dialog.')


class OpenstackSection(FeatureSection):
    class Meta:
        key = 'openstack'
        description = 'OpenStack resources provisioning'

    volume_types = Feature(
        'Allow to select OpenStack volume type when instance or volume is provisioned.'
    )


class RancherSection(FeatureSection):
    class Meta:
        key = 'rancher'
        description = 'Rancher resources provisioning'

    volume_mount_point = Feature(
        'Allow to select mount point for data volume when Rancher cluster is provisioned.'
    )


class SlurmSection(FeatureSection):
    class Meta:
        key = 'slurm'
        description = 'SLURM resources provisioning'

    jobs = Feature(
        'Render list of SLURM jobs as a separate tab in allocation details page.'
    )
