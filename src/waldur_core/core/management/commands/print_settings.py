import pprint
from datetime import timedelta

from django.core.management.base import BaseCommand

from waldur_core.core.metadata import WaldurConfiguration


def print_section(section, section_name, print_default=False):
    print(f'#### {section_name}')
    print()
    if print_default and section.default is not None and section.default != '':
        default_value = section.default
        if isinstance(default_value, timedelta):
            default_value = repr(default_value)
        print(f'Type: {section._type_display()}, default value: {default_value}')
    else:
        print(f'Type: {section._type_display()}')
    if section.field_info.description:
        print()
        print(section.field_info.description)
    print()


class Command(BaseCommand):
    def handle(self, *args, **options):
        sorted_items = sorted(WaldurConfiguration().__fields__.items())
        nested = [
            (section_name, section)
            for (section_name, section) in sorted_items
            if hasattr(section.type_, '__fields__')
        ]
        flat = [
            (section_name, section)
            for (section_name, section) in sorted_items
            if not hasattr(section.type_, '__fields__')
        ]

        for (section_name, section) in nested:
            type_ = section.type_
            default_value = pprint.pformat(section.default.dict())
            print(f'## {section_name} plugin')
            print()
            print(
                f'Default value: \n```python\n{section_name} = {default_value}\n```\n'
            )
            for field_name, field in sorted(type_.__fields__.items()):
                print_section(field, field_name)

        print(f'## Other variables')
        print()
        for (section_name, section) in flat:
            print_section(section, section_name, print_default=True)
