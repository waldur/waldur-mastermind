import os

from django.core.management.base import BaseCommand
from django.core.files import File

from waldur_mastermind.marketplace.models import Category, Section, Attribute, AttributeOption


available_categories = {
    'compute': ('Compute', 'Computing services'),
    'storage': ('Storage', 'Data preservation'),
    'backup': ('Backup', 'Backup solution'),
    'security': ('Security', 'Security services'),
    'cms': ('CMS', 'Content Management Systems'),
    'hpc': ('HPC', 'High Performance Computing'),
    'operations': ('Operations', 'Reliable support'),
    'consultancy': ('Consultancy', 'Experts for hire'),
}

common_sections = {
    'Support': [
        ('email', 'E-mail', 'string'),
        ('phone', 'Phone', 'string'),
        ('portal', 'Support portal', 'string'),
        ('guide', 'User guide', 'string'),
    ],
    'Security': [
        ('certification', 'Certification', 'listattr'),
    ],
}

hpc_sections = {
    'system_information': [
        ('queuing_system', 'Queueing system', 'listattr'),
        ('home_space', 'Home space', 'string'),
        ('work_space', 'Work space', 'string'),
        ('linux_distro', 'Linux distribution', 'listattr'),
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
        ('applications', 'Applications', 'listattr'),
    ],
}

specific_sections = {
    'hpc': hpc_sections,
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
    'deployment_type': [
        ('appliance', 'Appliance (Managed)'),
        ('remote', 'Remote (SaaS)')
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
    'virtualization': [
        ('kvm', 'KVM'),
        ('xen', 'XEN'),
        ('vmware', 'VMware'),
        ('Baremetal', 'Baremetal'),
    ],
}


def populate_category(category_code, category, sections):
    for section_key in sections.keys():
        section_prefix = '%s_%s' % (category_code, section_key)
        sec, _ = Section.objects.get_or_create(key=section_prefix, title=section_key, category=category)
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


class Command(BaseCommand):
    help = 'Loads a categories for the Marketplace'

    missing_args_message = 'Please define at least one category to load, available are:\n%s' %\
                           '\n'.join(available_categories.keys())

    def add_arguments(self, parser):
        parser.add_argument('category', nargs='+', type=str, help='List of categories to load')

    def handle(self, *args, **options):

        all_categories = available_categories.keys()

        for category_short in options['category']:
            if category_short not in all_categories:
                self.stdout.write(self.style.WARNING('Category "%s" is not available' % category_short))
                continue
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

            self.stdout.write(self.style.SUCCESS('Loaded category %s' % category_short))
