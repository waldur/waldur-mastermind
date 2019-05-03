import os

from django.core.management.base import BaseCommand
from django.core.files import File

from waldur_mastermind.marketplace.models import Category, CategoryColumn, \
    Section, Attribute, AttributeOption


def merge_two_dicts(x, y):
    z = x.copy()   # start with x's keys and values
    z.update(y)    # modifies z with y's keys and values & returns None
    return z


def humanize(name):
    return name.replace("_", " ").capitalize()


available_categories = {
    'backup': ('Backup', 'Backup solution'),
    'consultancy': ('Consultancy', 'Experts for hire'),
    'collocation': ('Collocation', 'Collocation services'),
    'cms': ('CMS', 'Content Management Systems'),
    'db': ('Databases', 'Relational DBMS'),
    'email': ('E-mail', 'E-mail services'),
    'hpc': ('HPC', 'High Performance Computing'),
    'licenses': ('Licenses', 'Application and OS licenses'),
    'vm': ('VMs', 'Virtual machines'),
    'vpc': ('Private clouds', 'Virtual private clouds'),
    'network': ('Network', 'Network services'),
    'operations': ('Operations', 'Reliable support'),
    'security': ('Security', 'Security services'),
    'storage': ('Storage', 'Data preservation'),
    # devices
    'microscope': ('Microscope', 'Available microscopes'),
    'spectrometry': ('Spectrometry', 'Available spectrometers'),
}

category_columns = {
    'storage': [
        {
            'title': 'Size',
            'widget': 'filesize',
            'attribute': 'size',
        },
        {
            'title': 'Attached to',
            'widget': 'attached_instance',
        },
    ],
    'vm': [
        {
            'title': 'Internal IP',
            'attribute': 'internal_ips',
            'widget': 'csv',
        },
        {
            'title': 'External IP',
            'attribute': 'external_ips',
            'widget': 'csv',
        },
    ],
}

common_sections = {
    'Support': [
        ('email', 'E-mail', 'string'),
        ('phone', 'Phone', 'string'),
        ('portal', 'Support portal', 'string'),
        ('description', 'Description', 'string'),
        ('terms_of_services_link', 'ToS link', 'string'),
    ],
    'SLA_simple': [
        ('low_sla_response', 'Response time (low priority, mins)', 'integer'),
        ('medium_sla_response', 'Response time (medium priority, mins)', 'integer'),
        ('high_sla_response', 'Response time (high priority, mins)', 'integer'),
    ],
    'SLA': [
        ('low_sla_response_wh', 'Response time (low priority, working hours)', 'integer'),
        ('low_sla_resolution_wh', 'Resolution time (low priority, working hours)', 'integer'),
        ('low_sla_response_nwh', 'Response time (low priority, non-working hours)', 'integer'),
        ('low_sla_resolution_nwh', 'Resolution time (low priority, non-working hours)', 'integer'),

        ('medium_sla_response_wh', 'Response time (medium priority, working hours)', 'integer'),
        ('medium_sla_resolution_wh', 'Resolution time (medium priority, working hours)', 'integer'),
        ('medium_sla_response_nwh', 'Response time (medium priority, non-working hours)', 'integer'),
        ('medium_sla_resolution_nwh', 'Resolution time (medium priority, non-working hours)', 'integer'),

        ('high_sla_response_wh', 'Response time (high priority, working hours)', 'integer'),
        ('high_sla_resolution_wh', 'Resolution time (high priority, working hours)', 'integer'),
        ('high_sla_response_nwh', 'Response time (high priority, non-working hours)', 'integer'),
        ('high_sla_resolution_nwh', 'Resolution time (high priority, non-working hours)', 'integer'),
    ],
    'Security': [
        ('certification', 'Certification', 'list'),
    ],

    'Location': [
        ('address', 'Address', 'string')
    ]
}

hpc_sections = {
    'system_information': [
        ('queuing_system', 'Queueing system', 'list'),
        ('home_space', 'Home space', 'string'),
        ('work_space', 'Work space', 'string'),
        ('linux_distro', 'Linux distribution', 'list'),
    ],
    'node_information': [
        ('cpu', 'CPU model', 'choice'),
        ('gpu', 'GPU model', 'choice'),
        ('memory', 'Memory per node (GB)', 'integer'),
        ('local_disk', 'Local disk (GB)', 'integer'),
        ('interconnect', 'Interconnect', 'choice'),
        ('node_count', 'Node count', 'integer'),
    ],
    'performance': [
        ('tflops', 'Peak TFlop/s', 'integer'),
        ('linpack', 'Linpack TFlop/s', 'integer')
    ],
    'software': [
        ('applications', 'Applications', 'list'),
    ],
}

collocation_sections = {
    'features': [
        ('collocation_remote_access', 'Remote access', 'choice'),
        ('computing_network', 'Network access', 'list'),
        ('collocation_dimensions', 'Dimensions', 'list'),
        ('collocation_power', 'Power (A)', 'integer'),
        ('collocation_cages', 'Locked cages', 'boolean'),
    ],
}

spectrometry_sections = {
    'properties': [
        ('spectrometry_type', 'Type', 'choice'),
        ('spectrometry_spectrum', 'Spectrum', 'choice'),
    ],
    'model': [
        ('spectrometry_mark', 'Mark', 'string'),
        ('spectrometry_model', 'Model', 'string'),
        ('spectrometry_manufacturer', 'Manufacturer', 'string')
    ],
}

microscope_sections = {
    'model': [
        ('microscope_mark', 'Mark', 'string'),
        ('microscope_model', 'Model', 'string'),
        ('microscope_manufacturer', 'Manufacturer', 'string')
    ],
}

computing_common_sections = {
    'details': [
        ('virtualization', 'Virtualization', 'choice'),
        ('computing_network', 'Network', 'list'),
        ('ha', 'High Availability', 'boolean'),
        ('av_monitoring', 'Availability monitoring', 'boolean'),
    ],
    'application': [
        ('os', 'Operating system', 'list'),
        ('application', 'Application', 'list'),
    ],
}

vpc_sections = {
    # nothing yet, TBA
}

vm_sections = {
    'software': [
        ('antivirus', 'Antivirus', 'boolean'),
    ],
    'remote_access': [
        ('vm_remote_access', 'Remote access', 'list'),
        ('vm_access_level', 'Access level', 'choice'),
    ]
}

email_sections = {
    'software': [
        ('email_software', 'Software', 'choice'),
    ],
    'features': [
        ('delegated_domain_administration', 'Delegated domain administration', 'boolean'),
        ('calendar', 'Calendar management', 'boolean'),
        ('webchat', 'Webchat', 'boolean'),
    ]
}

storage_sections = {
    'details': [
        ('storage_type', 'Storage type', 'choice'),
    ],
    'access': [
        ('web_interface', 'Web interface', 'boolean'),
        ('api', 'API', 'boolean'),
        ('api_flavor', 'API flavor', 'list'),
    ],
    'encryption': [
        ('encryption_at_rest', 'Encryption at-rest', 'boolean'),
        ('encryption_in_transit', 'Encryption in-transit', 'boolean'),
    ]
}

common_expert_sections = {
    'Scope': [
        ('scope_of_services', 'Scope of services', 'list'),
    ]
}

operations_sections = {
    'Supported services': [
        ('os', 'Supported OS', 'list'),
        ('application', 'Supported applications', 'list'),
    ],
}

consultancy_sections = {
    # nothing yet, TBA
}

security_sections = {
    'Application': [
        ('security_application', 'Application', 'string'),
        ('hardware_module', 'Hardware module', 'boolean'),
        ('vendor_name', 'Vendor name', 'string'),
        ('application_version', 'Application version', 'string'),
    ],
    'Access': [
        ('security_access', 'Access', 'list'),
    ]
}

network_section = {
    'Technology': [
        ('computing_network', 'Connected network', 'list'),
        ('vpn_technology', 'VPN technology', 'list'),
    ]
}

specific_sections = {
    'collocation': collocation_sections,
    'computing': merge_two_dicts(computing_common_sections, vm_sections),
    'consultancy': merge_two_dicts(common_expert_sections, consultancy_sections),
    'email': email_sections,
    'hpc': hpc_sections,
    'microscope': microscope_sections,
    'operations': merge_two_dicts(common_expert_sections, operations_sections),
    'vm': merge_two_dicts(computing_common_sections, vm_sections),
    'vpc': merge_two_dicts(computing_common_sections, vpc_sections),
    'security': security_sections,
    'spectrometry': spectrometry_sections,
    'storage': storage_sections,
}

enums = {
    'languages': [
        ('et', 'Estonian'),
        ('en', 'English'),
        ('lv', 'Latvian'),
        ('lt', 'Lithuanian'),
        ('ru', 'Russian'),
        ('sw', 'Swedish'),
        ('fi', 'Finnish'),
    ],
    'collocation_dimensions': [
        ('600x800', '600 x 800'),
        ('600x1000', '600 x 1000'),
    ],
    'deployment_type': [
        ('appliance', 'Appliance (Managed)'),
        ('remote', 'Remote (SaaS)')
    ],
    'email_software': [
        ('zimbra', 'Zimbra'),
        ('ibm_lotus', 'IBM Lotus'),
    ],
    'workdays': [
        ('base', '5 days'),
        ('extended', '7 days'),
    ],
    'businesshours': [
        ('basehours', '8 hours'),
        ('extendedhours', '24 hours'),
    ],
    'priority': [
        ('eob', 'End-of-business day'),
        ('nbd', 'Next business day'),
    ],
    'certification': [
        ('iskel', 'ISKE L'),
        ('iskem', 'ISKE M'),
        ('iskeh', 'ISKE H'),
        ('iso27001', 'ISO27001'),
    ],
    'interconnect': [
        ('infiniband_fdr', 'Infiniband FDR'),
        ('infiniband_edr', 'Infiniband EDR'),
        ('Ethernet_1G', 'Ethernet 1G'),
        ('Ethernet_10G', 'Ethernet 10G'),
    ],
    'sla_response': [
        '1', '1 hour',
        '2', '2 hours',
        '3', '3 hours',
        'eob', 'End of business day',
    ],
    'virtualization': [
        ('kvm', 'KVM'),
        ('xen', 'XEN'),
        ('vmware', 'VMware'),
        ('Baremetal', 'Baremetal'),
    ],
    'spectrometry_type': [
        ('aas', 'Atomic Absorption Spectrometer'),
        ('spectrophotometer', 'Spectrophotometer'),
        ('spectrometers', 'Spectrometers'),
    ],
    'spectrometry_spectrum': [
        ('visible', 'Visible'),
        ('infrared', 'Infrared'),
    ],
    'scope_of_services': [
        ('analysis', 'Analysis'),
        ('implementation', 'Implementation'),
        ('design', 'Design'),
        ('deployment', 'Deployment'),
        ('issue_resolution', 'issue_resolution'),
        ('change_management', 'Change management'),
        ('disaster_recovery', 'Disaster recovery'),
    ],
    'security_access': [
        ('api', 'API'),
        ('offline', 'Offline'),
    ],
    'storage_type': [
        ('block', 'Block'),
        ('object', 'Object'),
        ('fs', 'Filesystem'),
    ],
    'computing_network': [
        ('private', 'Private (own)'),
        ('aso', 'ASO'),
        ('ddn', 'DDN'),
        ('ogn', 'OGN'),
        ('banglagovnet', 'BanglaGovNet'),
        ('public', 'Public Internet'),
    ],
    'vpn_technology': [
        ('ipsec', 'IPSEC'),
        ('gre+ipsec', 'GRE + IPSEC'),
    ],
    'vm_access_level': [
        ('root', 'Root / Administrator'),
        ('user', 'User level'),
    ],
    'api_flavor': [
        ('s3', 'S3'),
        ('swift', 'Swift'),
        ('custom', 'Custom'),
    ],
    'vm_remote_access': [
        ('console', 'Console'),
        ('ssh_rdp', 'SSH/RDP'),
        ('direct_access', 'Direct access'),
    ],
    'collocation_remote_access': [
        ('vpn', 'VPN'),
    ],
    'os': [
        ('ubuntu16.04', 'Ubuntu 16.04'),
        ('centos7', 'CentOS 7'),
        ('windows2016', 'Windows 2016'),
        ('rhel7', 'RHEL 7'),
    ],
    'application': [
        ('zevenet', 'Zevenet'),
        ('owncloud', 'Owncloud'),
        ('lamp', 'LAMP'),
        ('nginx', 'Nginx'),
        ('iis', 'IIS'),
        ('mssql2017', 'MS SQL Server 2017'),
        ('mysql57', 'MySQL 5.7'),
    ],
}


def populate_category(category_code, category, sections):
    for section_key in sections.keys():
        section_prefix = '%s_%s' % (category_code, section_key)
        sec, _ = Section.objects.get_or_create(key=section_prefix, title=humanize(section_key), category=category)
        sec.is_standalone = True
        sec.save()
        for attribute in sections[section_key]:
            key, title, attribute_type = attribute
            attr, _ = Attribute.objects.get_or_create(key='%s_%s' % (section_prefix, key),
                                                      title=title, type=attribute_type, section=sec)
            if key in enums:
                values = enums[key]
                for val_key, val_label in values:
                    AttributeOption.objects.get_or_create(
                        attribute=attr, key='%s_%s_%s' % (section_prefix, key, val_key), title=val_label)


def load_category(category_short):
    category_name, category_description = available_categories[category_short]
    new_category, _ = Category.objects.get_or_create(title=category_name, description=category_description)
    category_icon = '%s.svg' % category_short
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'category_icons/')
    new_category.icon.save(category_icon,
                           File(open(path + category_icon, 'r')))
    new_category.save()
    # populate category with common section
    populate_category(category_short, new_category, common_sections)
    # add specific sections
    if category_short in specific_sections.keys():
        populate_category(category_short, new_category, specific_sections[category_short])

    # add category columns
    columns = category_columns.get(category_short, [])
    for index, attribute in enumerate(columns):
        CategoryColumn.objects.get_or_create(
            category=new_category,
            title=attribute['title'],
            defaults=dict(
                index=index,
                attribute=attribute.get('attribute', ''),
                widget=attribute.get('widget'),
            )
        )
    return new_category


class Command(BaseCommand):
    help = 'Loads a categories for the Marketplace'

    missing_args_message = 'Please define at least one category to load, available are:\n%s' %\
                           '\n'.join(available_categories.keys())

    def add_arguments(self, parser):
        parser.add_argument('category', nargs='+', type=str, help='List of categories to load')
        parser.add_argument('--basic_sla', nargs='?', type=bool, default=False, help='Use basic SLA for categories')

    def handle(self, *args, **options):

        all_categories = available_categories.keys()
        if options['basic_sla']:
            del common_sections['SLA']
        for category_short in options['category']:
            if category_short not in all_categories:
                self.stdout.write(self.style.WARNING('Category "%s" is not available' % category_short))
                continue
            new_category = load_category(category_short)
            self.stdout.write(self.style.SUCCESS('Loaded category %s, %s ' % (category_short, new_category.uuid)))
